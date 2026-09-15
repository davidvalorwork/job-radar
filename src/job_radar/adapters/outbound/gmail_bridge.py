"""Gmail connector adapter. Local request/response relay; no credential extraction.

The relay runs in an already authorized host (for example Codex), not unattended.
Each unique request is dispatched at most once. Responses contain provider evidence.
"""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from job_radar.adapters.outbound.bridge_errors import ERROR_CODES
from job_radar.domain.mail import (
    DeliveryBlocked,
    DeliveryUnknown,
    Mail,
    Receipt,
    SentRecord,
    address,
)


class Connector(Protocol):
    def call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class FileConnector:
    METHODS = {
        "gmail_get_profile",
        "gmail_search_emails",
        "gmail_send_email",
        "gmail_create_draft",
        "gmail_read_email",
    }

    def __init__(self, directory: Path, timeout: float = 90) -> None:
        if not 0 < timeout <= 300:
            raise ValueError("Bridge timeout must be 0..300 seconds")
        self.directory = directory / uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=False)
        self.timeout = timeout

    def call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if method not in self.METHODS:
            raise ValueError("Method not allowlisted")
        request_id = uuid4().hex
        request = self.directory / f"{request_id}.request.json"
        response = self.directory / f"{request_id}.response.json"
        pending = self.directory / f"{request_id}.tmp"
        pending.write_text(
            json.dumps(
                {
                    "id": request_id,
                    "method": method,
                    "arguments": arguments,
                    "expires_at": time.time() + self.timeout,
                }
            ),
            encoding="utf-8",
        )
        pending.replace(request)
        deadline = time.monotonic() + self.timeout
        while not response.exists():
            if time.monotonic() >= deadline:
                raise DeliveryUnknown("connector_timeout")
            time.sleep(0.1)
        with response.open("rb") as stream:
            payload = stream.read(4_194_305)
        if len(payload) > 4_194_304:
            raise DeliveryUnknown("connector_oversized_response")
        data = json.loads(payload)
        if (
            isinstance(data, dict)
            and data.get("id") == request_id
            and data.get("error")
            and isinstance(data.get("error_code"), str)
            and data["error_code"] in ERROR_CODES
        ):
            raise DeliveryUnknown("connector_" + data["error_code"])
        if (
            not isinstance(data, dict)
            or data.get("id") != request_id
            or data.get("error")
            or not isinstance(data.get("result"), dict)
        ):
            raise DeliveryUnknown("connector_invalid_response")
        return dict(data["result"])


class GmailBridge:
    def __init__(self, connector: Connector) -> None:
        self.connector = connector

    def profile(self) -> str:
        return address(self.connector.call("gmail_get_profile", {})["email"])

    def search(self, query: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        token = None
        seen_tokens = set()
        for _ in range(20):
            arguments: dict[str, Any] = {"query": query, "max_results": 100}
            if token:
                arguments["next_page_token"] = token
            result = self.connector.call("gmail_search_emails", arguments)
            records.extend(result["emails"])
            token = result.get("next_page_token")
            if not token:
                return records
            if token in seen_tokens:
                break
            seen_tokens.add(token)
        raise DeliveryBlocked("incomplete_history")

    def recent_sent(self, since: datetime) -> list[SentRecord]:
        records = []
        seen = set()
        for item in self.search(f"in:sent after:{int(since.timestamp()) - 1}"):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if "SENT" not in item["labels"] or not item.get("email_ts"):
                raise DeliveryBlocked("invalid_history")
            at = datetime.fromisoformat(item["email_ts"])
            recipients = tuple(
                address(value) for value in item["to"] + item.get("cc", []) + item.get("bcc", [])
            )
            if not recipients:
                raise DeliveryBlocked("invalid_history")
            records.append(SentRecord(item["id"], recipients, at))
        return records

    def contact_block(self, mail: Mail) -> str | None:
        # All ages: old SENT alone never suppresses, but explicit opt-outs/bounces persist.
        query = f'in:anywhere {{"{mail.recipient}" "{mail.company_key}"}}'
        for item in self.search(query):
            if "DRAFT" in item["labels"]:
                # Discarded drafts are not pending applications. Keep searching
                # all other mail, including trashed opt-outs and hard bounces.
                if "TRASH" in item["labels"]:
                    continue
                return "existing_draft_needs_review"
            if "SENT" in item["labels"]:
                continue
            result = self.connector.call(
                "gmail_read_email", {"message_id": item["id"], "format": "full"}
            )
            text = json.dumps(result.get("payload", {}), ensure_ascii=False).lower()
            if re.search(
                r"do not (?:email|contact)|stop (?:emailing|contacting)|remove me|"
                r"marked as spam|reported as spam|no (?:me |nos )?contact[ea]|"
                r"address not found|recipient address rejected|user unknown|5\.1\.1",
                text,
            ):
                return "provider_suppression"
        return None

    def draft_exists(self, mail: Mail) -> bool:
        # A program-created draft is tracked locally too. This additionally prevents
        # placing a second draft over one the user already created in Gmail.
        return bool(self.search(f"in:drafts to:{mail.recipient}"))

    @staticmethod
    def _payload(mail: Mail) -> dict[str, Any]:
        return {
            "to": mail.recipient,
            "from_address": mail.account,
            "subject": mail.subject,
            "payload": {
                "mime_type": "multipart/mixed",
                "parts": [
                    {
                        "mime_type": "text/plain",
                        "charset": "UTF-8",
                        "body": {"content": mail.body},
                    },
                    {
                        "mime_type": "application/pdf",
                        "filename": mail.attachment_name,
                        "content_disposition": "attachment",
                        "body": {"base64_url_content": mail.attachment_base64},
                    },
                ],
            },
            "response_fields": ["id", "thread_id", "label_ids"],
        }

    def send(self, mail: Mail) -> str:
        result = self.connector.call("gmail_send_email", self._payload(mail))
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise DeliveryUnknown("missing_provider_id")
        return str(result["id"])

    def draft(self, mail: Mail) -> str:
        result = self.connector.call("gmail_create_draft", self._payload(mail))
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise DeliveryUnknown("missing_provider_id")
        return str(result["id"])

    def verify(self, mail: Mail, message_id: str) -> Receipt:
        result = self.connector.call(
            "gmail_read_email", {"message_id": message_id, "format": "full"}
        )
        payload = result.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        parts = payload.get("parts", [])
        bodies = [
            p.get("body", {}).get("content", "")
            for p in parts
            if p.get("mime_type") == "text/plain"
        ]
        attachments = [p for p in parts if p.get("filename")]
        if (
            result.get("id") != message_id
            or "SENT" not in result.get("label_ids", [])
            or headers.get("to", "").lower() != mail.recipient
            or headers.get("from", "").lower() != mail.account
            or headers.get("cc")
            or headers.get("bcc")
            or headers.get("subject") != mail.subject
            or len(bodies) != 1
            or bodies[0].replace("\r\n", "\n").rstrip("\n")
            != mail.body.replace("\r\n", "\n").rstrip("\n")
            or len(attachments) != 1
            or attachments[0].get("filename") != mail.attachment_name
            or attachments[0].get("mime_type") != "application/pdf"
            or attachments[0].get("body", {}).get("size") != mail.attachment_size
            or (
                attachments[0].get("body", {}).get("sha256") is not None
                and attachments[0]["body"]["sha256"] != mail.attachment_sha256
            )
        ):
            raise DeliveryUnknown("send_verification_mismatch")
        at = datetime.fromtimestamp(int(result["internal_date"]) / 1000, UTC)
        if at > datetime.now(UTC) + timedelta(minutes=1):
            raise DeliveryUnknown("invalid_provider_time")
        return Receipt(message_id, at)
