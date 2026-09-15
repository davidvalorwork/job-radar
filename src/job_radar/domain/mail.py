"""Immutable, authorized delivery plans. No credential or transport dependencies."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from json import dumps

# Local safety ceiling; Gmail/provider quotas are separate and account-specific.
MAX_DAILY_SEND_LIMIT = 500


def address(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value):
        raise ValueError("A single plain email address is required")
    if len(value) > 254:
        raise ValueError("Email address too long")
    return value


def digest(value: object) -> str:
    return sha256(dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Mail:
    account: str
    recipient: str
    company_key: str
    subject: str
    body: str
    job_url: str
    contact_source_url: str
    verified_at: str
    attachment_name: str
    attachment_base64: str
    attachment_size: int
    attachment_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account", address(self.account))
        object.__setattr__(self, "recipient", address(self.recipient))
        if self.company_key.startswith("contact:"):
            # Independent recruiters on shared mail hosts must not become one company.
            if address(self.company_key.removeprefix("contact:")) != self.recipient:
                raise ValueError("Contact identity must match recipient")
        elif not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", self.company_key):
            raise ValueError("Canonical company domain required")
        if not self.subject.strip() or any(c in self.subject for c in "\r\n\x00"):
            raise ValueError("Invalid mail subject")
        if not self.body.strip() or "\x00" in self.body:
            raise ValueError("Invalid mail body")
        if datetime.fromisoformat(self.verified_at).utcoffset() is None:
            raise ValueError("Evidence time must include timezone")

    @property
    def key(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class SentRecord:
    id: str
    recipients: tuple[str, ...]
    at: datetime


@dataclass(frozen=True)
class Receipt:
    id: str
    at: datetime


class DeliveryBlocked(RuntimeError):
    """A guard prevents sending; no transport should be called."""


class DeliveryUnknown(RuntimeError):
    """An operation may have delivered. Never retry it blindly."""
