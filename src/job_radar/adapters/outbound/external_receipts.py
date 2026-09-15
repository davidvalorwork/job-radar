"""Bounded, explicitly reviewed provider metadata exports; never sends or invents plans."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from hashlib import sha256
from pathlib import Path

from pydantic import AwareDatetime, Field

from job_radar.adapters.outbound.config import StrictModel
from job_radar.domain.mail import address, digest


class ExpectedMessage(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    recipient: str = Field(max_length=254)
    company_key: str = Field(max_length=253)
    subject: str = Field(min_length=1, max_length=998)


class Header(StrictModel):
    name: str = Field(max_length=256)
    value: str = Field(max_length=8192)


class ObservedMessage(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    label_ids: list[str] = Field(max_length=100)
    internal_date: str = Field(pattern=r"^\d{1,15}$")
    headers: list[Header] = Field(max_length=100)


class ExternalExport(StrictModel):
    account: str = Field(max_length=254)
    observed_at: AwareDatetime
    expected: list[ExpectedMessage] = Field(min_length=1, max_length=500)
    messages: list[ObservedMessage] = Field(min_length=1, max_length=500)


def external_plan(path: Path, now: datetime) -> tuple[str, str, list[dict[str, str]]]:
    with path.open("rb") as stream:
        data = stream.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("External export exceeds 4 MB")
    export = ExternalExport.model_validate_json(data)
    account = address(export.account)
    if (
        now.utcoffset() is None
        or export.observed_at > now + timedelta(minutes=1)
        or now - export.observed_at > timedelta(hours=24)
    ):
        raise ValueError("Read-back export is stale or has an invalid clock")
    expected = {item.id: item for item in export.expected}
    if len(expected) != len(export.expected) or Counter(
        item.id for item in export.messages
    ) != Counter(expected.keys()):
        raise ValueError("Exact complete distinct provider IDs required")
    rows = []
    for message in export.messages:
        item = expected[message.id]
        headers: dict[str, list[str]] = {}
        for header in message.headers:
            headers.setdefault(header.name.lower(), []).append(header.value)
        if any(len(headers.get(name, [])) != 1 for name in ("from", "to", "subject")):
            raise ValueError("Required headers must occur exactly once")
        sender = [address(value) for _, value in getaddresses(headers["from"])]
        recipients = [address(value) for _, value in getaddresses(headers["to"])]
        if (
            sender != [account]
            or recipients != [address(item.recipient)]
            or any(value.strip() for name in ("cc", "bcc") for value in headers.get(name, []))
            or headers["subject"] != [item.subject]
            or "SENT" not in message.label_ids
            or "DRAFT" in message.label_ids
        ):
            raise ValueError("Provider read-back does not match expected sent message")
        try:
            at = datetime.fromtimestamp(int(message.internal_date) / 1000, UTC)
        except (ValueError, OverflowError, OSError) as error:
            raise ValueError("Invalid provider timestamp") from error
        if at > export.observed_at + timedelta(minutes=1):
            raise ValueError("Provider timestamp is in the future")
        rows.append(
            {
                "id": item.id,
                "recipient": address(item.recipient),
                "company_key": item.company_key,
                "at": at.isoformat(),
                "subject_sha256": sha256(item.subject.encode()).hexdigest(),
            }
        )
    return digest(export.model_dump(mode="json")), account, rows
