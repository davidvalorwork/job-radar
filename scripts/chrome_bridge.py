"""Local, authenticated Chrome relay. No Google API, AI calls or cookie access.

Start explicitly; sending is disabled unless --enable-send is supplied. The Chrome
extension processes the existing FileConnector protocol, preserving the outbox.
"""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from contextlib import suppress
from datetime import UTC
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from job_radar.adapters.outbound.bridge_errors import safe_error, safe_phase

METHODS = {
    "gmail_get_profile",
    "gmail_search_emails",
    "gmail_read_email",
    "gmail_send_email",
    "gmail_create_draft",
}
LIMIT = 24 * 1024 * 1024
ID = re.compile(r"[a-f0-9]{32}")
ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")


def original(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize actual Gmail Show original MIME; never infer content from a request."""
    identifier = record["id"]
    if not re.fullmatch(r"[a-f0-9]{8,32}", identifier):
        raise ValueError("invalid_message_id")
    raw = record["raw"].encode("utf-8")
    if len(raw) > LIMIT:
        raise ValueError("original_too_large")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.get("Message-ID") or not message.get("Date") or message.defects:
        raise ValueError("incomplete_original")
    at = parsedate_to_datetime(message["Date"])
    if at.tzinfo is None:
        raise ValueError("missing_timezone")
    headers = []
    recipients: dict[str, list[str]] = {}
    for name in ("From", "To", "Cc", "Bcc"):
        values = [addr.lower() for _, addr in getaddresses(message.get_all(name, []))]
        recipients[name.lower()] = values
        headers.append({"name": name, "value": ", ".join(values)})
    headers.append({"name": "Subject", "value": str(message.get("Subject", ""))})
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b""
        if not isinstance(content, bytes):
            raise ValueError("invalid_mime_part")
        item: dict[str, Any] = {"mime_type": part.get_content_type()}
        if part.get_filename():
            item.update(
                filename=part.get_filename(),
                body={
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            )
        elif part.get_content_type() == "text/plain":
            item["body"] = {"content": content.decode(part.get_content_charset() or "utf-8")}
        else:
            continue
        parts.append(item)
    labels = record.get("labels", [])
    if not isinstance(labels, list) or any(
        x not in {"SENT", "INBOX", "DRAFT", "TRASH"} for x in labels
    ):
        raise ValueError("invalid_labels")
    return {
        "id": identifier,
        "label_ids": labels,
        "labels": labels,
        # UI originals expose the RFC Date, not Gmail API internalDate.
        "internal_date": int(at.timestamp() * 1000),
        "timestamp_source": "rfc822_date",
        "email_ts": at.astimezone(UTC).isoformat(),
        "to": recipients["to"],
        "cc": recipients["cc"],
        "bcc": recipients["bcc"],
        "payload": {"headers": headers, "parts": parts},
    }


def normalize(method: str, result: dict[str, Any]) -> dict[str, Any]:
    if method == "gmail_read_email":
        return original(result)
    if method == "gmail_search_emails":
        # A partial UI search is never represented as a complete empty history.
        if result.get("complete") is not True:
            raise ValueError("incomplete_history")
        emails = []
        for record in result["emails"]:
            if record.get("labels") == ["DRAFT"]:
                emails.append({"id": record["id"], "labels": ["DRAFT"]})
            else:
                item = original(record)
                emails.append(
                    {key: item[key] for key in ("id", "labels", "email_ts", "to", "cc", "bcc")}
                )
        return {"emails": emails}
    if method == "gmail_get_profile":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result.get("email", "")):
            raise ValueError("invalid_account")
    elif method in {"gmail_send_email", "gmail_create_draft"} and not re.fullmatch(
        r"[a-f0-9]{8,32}", result.get("id", "")
    ):
        raise ValueError("missing_provider_id")
    return result


class Bridge:
    def __init__(self, directory: Path, account: str, *, enable_send: bool = False) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.account = account.lower()
        self.enable_send = enable_send
        self.token = secrets.token_urlsafe(32)
        self.origin: str | None = None
        self.lock = threading.Lock()
        self.active: tuple[Path, dict[str, Any]] | None = None
        self.halted = False
        self.completed = 0
        self.worker_version: str | None = None
        self.last_worker_seen = 0.0

    def observe_worker(self, value: Any) -> None:
        """Log authenticated connection changes, never every poll or private payloads."""
        version = (
            value
            if isinstance(value, str) and re.fullmatch(r"\d{1,5}(?:\.\d{1,5}){2,3}", value)
            else "unknown"
        )
        now = time.monotonic()
        with self.lock:
            changed = version != self.worker_version or now - self.last_worker_seen > 30
            self.worker_version = version
            self.last_worker_seen = now
            if changed:
                print(
                    json.dumps(
                        {
                            "event": "worker_connected",
                            "version": version,
                            "sending": self.enable_send,
                        }
                    ),
                    flush=True,
                )

    def authorized(self, origin: str, token: str) -> bool:
        if not ORIGIN.fullmatch(origin) or not secrets.compare_digest(token, self.token):
            return False
        with self.lock:
            if self.origin is None:
                self.origin = origin
            return self.origin == origin

    @staticmethod
    def respond(path: Path, response: dict[str, Any]) -> None:
        target = path.with_name(path.name.replace(".request.json", ".response.json"))
        temporary = target.with_suffix(".tmp")
        data = json.dumps(response, ensure_ascii=True)
        if len(data.encode()) > 4_194_304:
            data = json.dumps({"id": response["id"], "error": "response_too_large"})
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(target)

    def take(self) -> dict[str, Any]:
        with self.lock:
            if self.halted:
                return {"halted": True}
            if self.active:
                path, request = self.active
                if request["expires_at"] <= time.time():
                    self.respond(path, {"id": request["id"], "error": "expired_unknown"})
                    self.halted = True
                    return {"halted": True}
                return {"busy": True}
            # Only exact UUID child directories; never traverse symlinks or arbitrary paths.
            for folder in sorted(self.directory.iterdir()):
                if not ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                    continue
                for path in sorted(folder.glob("*.request.json")):
                    identifier = path.name.removesuffix(".request.json")
                    if not ID.fullmatch(identifier) or path.is_symlink():
                        continue
                    if path.with_name(f"{identifier}.response.json").exists():
                        continue
                    if path.stat().st_size > LIMIT:
                        self.respond(path, {"id": identifier, "error": "request_too_large"})
                        continue
                    try:
                        request = json.loads(path.read_text(encoding="utf-8"))
                        if (
                            request["id"] != identifier
                            or request["method"] not in METHODS
                            or not isinstance(request["arguments"], dict)
                            or not isinstance(request["expires_at"], (float, int))
                            or not math.isfinite(request["expires_at"])
                            or request["expires_at"] > time.time() + 301
                        ):
                            raise ValueError("invalid_request")
                    except ValueError, KeyError, TypeError:
                        self.respond(path, {"id": identifier, "error": "invalid_request"})
                        continue
                    claim = path.with_name(f"{identifier}.dispatched")
                    if claim.exists():
                        self.respond(path, {"id": identifier, "error": "previous_dispatch_unknown"})
                        self.halted = True
                        return {"halted": True}
                    if request["expires_at"] <= time.time():
                        self.respond(path, {"id": identifier, "error": "expired"})
                        continue
                    if request["method"] in {"gmail_send_email", "gmail_create_draft"}:
                        if request["arguments"].get("from_address", "").lower() != self.account:
                            self.respond(path, {"id": identifier, "error": "send_not_authorized"})
                            continue
                        if request["method"] == "gmail_send_email" and not self.enable_send:
                            self.respond(path, {"id": identifier, "error": "send_not_authorized"})
                            continue
                    # Exclusive durable claim before handing a side effect to Chrome.
                    with claim.open("x", encoding="ascii") as stream:
                        stream.write(identifier)
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.active = (path, request)
                    return {
                        "request": request,
                        "account": self.account,
                        "enable_send": self.enable_send,
                    }
            return {"idle": True}

    def finish(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if not self.active or data.get("id") != self.active[1]["id"] or self.halted:
                raise ValueError("unexpected_response")
            path, request = self.active
            response: dict[str, Any] = {"id": request["id"]}
            try:
                if request["expires_at"] <= time.time() or data.get("error"):
                    raise ValueError("browser_operation_failed_or_expired")
                response["result"] = normalize(request["method"], data["result"])
                if (
                    request["method"] == "gmail_get_profile"
                    and response["result"]["email"].lower() != self.account
                ):
                    raise ValueError("account_mismatch")
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                response = {
                    "id": request["id"],
                    "error": "browser_result_invalid",
                    "error_code": safe_error(data.get("error") or str(error)),
                    "phase": safe_phase(data.get("phase")),
                }
                self.halted = True
            self.respond(path, response)
            self.active = None
            self.completed += 1
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "method": request["method"],
                        "ok": "error" not in response,
                        "completed": self.completed,
                        **(
                            {"error_code": response["error_code"], "phase": response["phase"]}
                            if "error_code" in response
                            else {}
                        ),
                    }
                ),
                flush=True,
            )
            return {
                "accepted": True,
                "halted": self.halted,
                **(
                    {"error_code": response["error_code"], "phase": response["phase"]}
                    if "error_code" in response
                    else {}
                ),
            }


def handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Do not log payloads, tokens, mailbox data or query strings.

        def reply(self, code: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            origin = self.headers.get("Origin", "")
            if ORIGIN.fullmatch(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            if not ORIGIN.fullmatch(self.headers.get("Origin", "")):
                self.reply(403, {"error": "forbidden"})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.end_headers()

        def do_POST(self) -> None:
            self.connection.settimeout(15)
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":  # type: ignore[attr-defined]
                self.reply(403, {"error": "invalid_host"})
                return
            if not bridge.authorized(
                self.headers.get("Origin", ""),
                self.headers.get("Authorization", "").removeprefix("Bearer "),
            ):
                self.reply(403, {"error": "forbidden"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= LIMIT or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid_size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("invalid_body")
                if self.path == "/next":
                    bridge.observe_worker(data.get("worker_version"))
                    result = bridge.take()
                elif self.path == "/result":
                    result = bridge.finish(data)
                else:
                    self.reply(404, {"error": "not_found"})
                    return
                self.reply(200, result)
            except ValueError, OSError, KeyError, TypeError:
                self.reply(400, {"error": "invalid_request"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, default=Path(".local/chrome/bridge"))
    parser.add_argument("--pairing", type=Path, default=Path(".local/chrome/pairing.json"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--enable-send", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", args.account):
        parser.error("Invalid account")
    bridge = Bridge(args.bridge_dir, args.account, enable_send=args.enable_send)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler(bridge)) as server:
        args.pairing.parent.mkdir(parents=True, exist_ok=True)
        args.pairing.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{server.server_port}",
                    "token": bridge.token,
                    "account": bridge.account,
                }
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {"pairing_file": str(args.pairing), "sending": args.enable_send, "llm_calls": 0}
            ),
            flush=True,
        )
        with suppress(KeyboardInterrupt):
            server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
