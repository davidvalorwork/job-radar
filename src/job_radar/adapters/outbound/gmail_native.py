"""Direct Gmail transport: SDK OAuth + batched metadata, no assistant or LLM calls."""

import base64
import importlib
import json
import os
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from job_radar.domain.mail import DeliveryBlocked, DeliveryUnknown, address

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def inspect_credentials(path: Path) -> dict[str, Any]:
    """Check local setup only: never refresh a token, access Gmail, or reveal secret values."""
    configured = False
    reason = "missing_credentials"
    try:
        if path.stat().st_size > 65536:
            raise ValueError("Oversized credentials")
        data = json.loads(path.read_text(encoding="utf-8"))
        configured = (
            isinstance(data, dict)
            and all(
                isinstance(data.get(k), str) and data[k].strip()
                for k in ("refresh_token", "client_id", "client_secret")
            )
            and isinstance(data.get("scopes"), list)
            and set(SCOPES).issubset(data["scopes"])
        )
        reason = "provider_authorization_not_checked" if configured else "incomplete_credentials"
    except FileNotFoundError:
        pass
    except OSError, ValueError, TypeError:
        reason = "unreadable_credentials"
    return {
        "status": "configured_unverified" if configured else "oauth_setup_required",
        "local_credentials_configured": configured,
        "reason": reason,
        "provider_verified": False,
        "network_enabled": False,
        "sending_enabled": False,
        "transport": "gmail_oauth",
    }


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise DeliveryBlocked("install_gmail_extra") from error


def _service(credentials: Any) -> Any:
    http = _module("google_auth_httplib2").AuthorizedHttp(
        credentials, http=_module("httplib2").Http(timeout=30)
    )
    return _module("googleapiclient.discovery").build(
        "gmail", "v1", http=http, cache_discovery=False, static_discovery=True
    )


def authorize(client_secret: Path, token_path: Path, expected_account: str) -> None:
    """Explicit interactive setup only. Never runs implicitly during batch delivery."""
    try:
        _authorize(client_secret, token_path, expected_account)
    except DeliveryBlocked:
        raise
    except Exception as error:
        raise DeliveryBlocked("gmail_authorization_failed") from error


def _authorize(client_secret: Path, token_path: Path, expected_account: str) -> None:
    expected_account = address(expected_account)
    if token_path.exists():
        raise DeliveryBlocked("credentials_exist_use_a_new_path")
    flow = _module("google_auth_oauthlib.flow").InstalledAppFlow.from_client_secrets_file(
        str(client_secret), SCOPES
    )
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
        timeout_seconds=180,
        authorization_prompt_message="Authorize Job Radar in your browser.",
        success_message="Authorization received. You can close this window.",
        access_type="offline",
        prompt="consent",
        login_hint=expected_account,
    )
    service = _service(credentials)
    try:
        if (
            address(service.users().getProfile(userId="me").execute()["emailAddress"])
            != expected_account
        ):
            raise DeliveryBlocked("account_mismatch")
        if not credentials.refresh_token or not credentials.has_scopes(SCOPES):
            raise DeliveryBlocked("missing_refresh_token_or_scopes")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids overwriting another account; never log token content.
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(credentials.to_json())
    finally:
        service.close()


def connect(token_path: Path) -> NativeGmailConnector:
    try:
        credentials = _module("google.oauth2.credentials").Credentials.from_authorized_user_file(
            str(token_path), SCOPES
        )
        if not credentials.refresh_token or not credentials.has_scopes(SCOPES):
            raise DeliveryBlocked("missing_refresh_token_or_scopes")
        return NativeGmailConnector(_service(credentials))
    except DeliveryBlocked:
        raise
    except Exception as error:
        raise DeliveryBlocked("gmail_credentials_unavailable") from error


def _payload(part: dict[str, Any]) -> dict[str, Any]:
    body = part.get("body", {})
    normalized = {"size": body.get("size", 0)}
    if (data := body.get("data")) and part.get("mimeType", "").startswith("text/"):
        headers = EmailMessage()
        for header in part.get("headers", []):
            if header["name"].lower() == "content-type":
                headers["Content-Type"] = header["value"]
        normalized["content"] = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            headers.get_content_charset() or "utf-8", errors="replace"
        )
    return {
        "mime_type": part.get("mimeType"),
        "filename": part.get("filename", ""),
        "headers": part.get("headers", []),
        "body": normalized,
        "parts": [_payload(child) for child in part.get("parts", [])],
    }


class NativeGmailConnector:
    """Implements the existing connector contract while keeping provider IO local."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.metadata: dict[str, dict[str, Any]] = {}
        self.metrics = {"provider_round_trips": 0, "metadata_cache_hits": 0, "llm_calls": 0}

    def close(self) -> None:
        self.service.close()

    def _execute(self, request: Any) -> dict[str, Any]:
        self.metrics["provider_round_trips"] += 1
        try:
            return dict(request.execute(num_retries=0))
        except Exception as error:
            raise DeliveryBlocked("gmail_request_failed") from error

    def _metadata(self, ids: list[str]) -> list[dict[str, Any]]:
        found = {key: self.metadata[key] for key in ids if key in self.metadata}
        self.metrics["metadata_cache_hits"] += len(found)
        missing = list(dict.fromkeys(key for key in ids if key not in found))
        errors: list[str] = []

        def receive(key: str, response: Any, exception: Exception | None) -> None:
            if exception or not isinstance(response, dict) or response.get("id") != key:
                errors.append(key)
            else:
                found[key] = response
                # Only immutable SENT metadata is reusable; drafts never enter this cache.
                if "SENT" in response.get("labelIds", []):
                    self.metadata[key] = response

        for offset in range(0, len(missing), 50):
            batch = self.service.new_batch_http_request(callback=receive)
            for key in missing[offset : offset + 50]:
                batch.add(
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=key,
                        format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Bcc", "Subject"],
                        fields="id,labelIds,internalDate,snippet,payload/headers",
                    ),
                    request_id=key,
                )
            self.metrics["provider_round_trips"] += 1
            try:
                batch.execute()
            except Exception as error:
                raise DeliveryBlocked("incomplete_history") from error
        if errors or any(key not in found for key in ids):
            raise DeliveryBlocked("incomplete_history")
        return [found[key] for key in ids]

    def call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        messages = self.service.users().messages()
        if method == "gmail_get_profile":
            return {
                "email": self._execute(self.service.users().getProfile(userId="me"))["emailAddress"]
            }
        if method == "gmail_search_emails":
            result = self._execute(
                messages.list(
                    userId="me",
                    q=arguments["query"],
                    maxResults=min(arguments.get("max_results", 100), 100),
                    pageToken=arguments.get("next_page_token"),
                    includeSpamTrash=True,
                    fields="messages/id,nextPageToken",
                )
            )
            emails = []
            for item in self._metadata([m["id"] for m in result.get("messages", [])]):
                headers = {
                    h["name"].lower(): h["value"]
                    for h in item.get("payload", {}).get("headers", [])
                }
                row = {
                    field: [addr for _, addr in getaddresses([headers.get(field, "")])]
                    for field in ("to", "cc", "bcc")
                }
                emails.append(
                    {
                        **row,
                        "id": item["id"],
                        "labels": item.get("labelIds", []),
                        "email_ts": datetime.fromtimestamp(
                            int(item["internalDate"]) / 1000, UTC
                        ).isoformat(),
                        "subject": headers.get("subject", ""),
                        "snippet": item.get("snippet", ""),
                    }
                )
            return {"emails": emails, "next_page_token": result.get("nextPageToken")}
        if method == "gmail_read_email":
            result = self._execute(
                messages.get(userId="me", id=arguments["message_id"], format="full")
            )
            return {
                "id": result["id"],
                "label_ids": result.get("labelIds", []),
                "internal_date": result["internalDate"],
                "payload": _payload(result["payload"]),
            }
        if method == "gmail_send_email":
            # Only the application's exact plain text + PDF contract is accepted.
            parts = arguments["payload"]["parts"]
            if len(parts) != 2 or arguments.get("cc") or arguments.get("bcc"):
                raise ValueError("Unsupported outgoing payload")
            message = EmailMessage()
            message["To"] = address(arguments["to"])
            message["From"] = address(arguments["from_address"])
            message["Subject"] = arguments["subject"]
            message.set_content(parts[0]["body"]["content"], charset="utf-8")
            message.add_attachment(
                base64.urlsafe_b64decode(parts[1]["body"]["base64_url_content"]),
                maintype="application",
                subtype="pdf",
                filename=parts[1]["filename"],
            )
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            try:
                return self._execute(messages.send(userId="me", body={"raw": raw}))
            except Exception as error:
                # In particular, never automatically retry a timeout/429/5xx on send.
                raise DeliveryUnknown("gmail_send_uncertain") from error
        raise ValueError("Unsupported Gmail operation")
