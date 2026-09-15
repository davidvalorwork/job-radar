"""Offline, bounded evidence packets. Extraction is never approval to send."""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from job_radar.adapters.outbound.config import read_yaml
from job_radar.adapters.outbound.posting_evidence import inspect_posting

EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SIGNALS = re.compile(
    r"@|\bremote\b|remot[oa]|worldwide|salary|salario|compensation|USD|\$\d|€|"
    r"only|excluyen|based in|location|ubicaci[oó]n|visa|citizen|"
    r"apply|postul|env[ií]|closed|cerrad|deadline|subject|asunto",
    re.I,
)
FOOTER = re.compile(
    r"(?im)^\s*(?:#{1,4}\s*)?(?:More relevant posts|Más publicaciones relevantes|"
    r"More from this author|Similar Jobs|\d+[\d,. ]*\s+Comments?|"
    r"\d+[\d,. ]*\s+comentarios)\b"
)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def card(row: sqlite3.Row) -> dict[str, Any] | None:
    text = row["content"]
    title = re.search(r"(?m)^Title:\s*(.+)", text)
    posting = inspect_posting(row["value"], text)
    body = posting["body"] if posting else text.split("Markdown Content:", 1)[-1]
    body = FOOTER.split(body, maxsplit=1)[0]
    body = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", body)
    body = re.sub(r"\[([^\]]*)\]\([^\n]*?\)", r"\1", body)
    heading = re.search(r"(?m)^#{1,3}\s+(.+)", body)
    contacts = sorted(
        {
            match.lower()
            for match in EMAIL.findall(body)
            if not match.lower().startswith(("privacy@", "noreply@", "no-reply@"))
        }
    )
    if not contacts:
        return None
    # Merge overlapping windows; never send an entire long line/navigation block to the model.
    spans: list[list[int]] = []
    for match in SIGNALS.finditer(body):
        start = max(0, match.start() - 65, body.rfind("\n", 0, match.start()) + 1)
        end = min(len(body), match.end() + 105)
        if spans and start <= spans[-1][1] and end - spans[-1][0] <= 260:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    # Contact invitations first, then salary/geography/context. These remain untrusted excerpts.
    spans.sort(key=lambda span: "@" not in body[span[0] : span[1]])
    excerpts = list(dict.fromkeys(" ".join(body[a:b].split()) for a, b in spans))[:5]
    return {
        "primary_url": posting["primary_url"] if posting else None,
        "source_flags": posting["flags"] if posting else [],
        "id": row["id"],
        "digest": row["digest"],
        "url": row["value"],
        "fetched_at": datetime.fromtimestamp(row["at"], UTC).isoformat(),
        "title": (title[1] if title else heading[1] if heading else "Untitled evidence")[:180],
        "contacts": contacts[:8],
        "contacts_truncated": len(contacts) > 8,
        "excerpts": excerpts,
        "requires_review": True,
        "official_application_contact": False,
        "suppression_reviewed": False,
    }


def exclusions(path: Path | None, now: datetime) -> tuple[set[str], set[str]]:
    if path is None:
        return set(), set()
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        recipients: set[str] = set()
        companies: set[str] = set()
        for kind, value in connection.execute("SELECT kind,value FROM suppressions"):
            (recipients if kind == "recipient" else companies).add(value.lower())
        for recipient, company, state, at in connection.execute(
            "SELECT recipient,company_key,state,sent_at FROM outbox WHERE state!='prepared'"
        ):
            if state != "sent" or datetime.fromisoformat(at) > now - timedelta(hours=48):
                recipients.add(recipient.lower())
                companies.add(company.lower())
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_sends'"
        ).fetchone():
            for recipient, company, at in connection.execute(
                "SELECT recipient,company_key,sent_at FROM external_sends"
            ):
                if datetime.fromisoformat(at) > now - timedelta(hours=48):
                    recipients.add(recipient.lower())
                    companies.add(company.lower())
        return recipients, companies
    finally:
        connection.close()


def review_packet(
    path: Path,
    *,
    limit: int = 25,
    max_chars: int = 12000,
    after: int = 0,
    snapshot: str | None = None,
    seen: tuple[Path, ...] = (),
    outbox: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 100 or not 2048 <= max_chars <= 64000 or after < 0:
        raise ValueError("Invalid review budget")
    if after and snapshot is None:
        raise ValueError("Resuming requires the previous snapshot fingerprint")
    known = set()
    for source in seen:
        previous = read_yaml(source)
        for item in previous["items"]:
            known.add((item["id"], item["digest"]))
    recipients, companies = exclusions(outbox, now or datetime.now(UTC))
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        fingerprint = hashlib.sha256()
        for row in db.execute("SELECT rowid,id,digest,at,status FROM evidence ORDER BY rowid"):
            fingerprint.update(encode(tuple(row)).encode())
        current = fingerprint.hexdigest()
        if snapshot is not None and snapshot != current:
            raise ValueError("Cache changed; restart with seen packets instead of a stale cursor")
        result: dict[str, Any] = {
            "items": [],
            "next_cursor": after,
            "snapshot": current,
            "has_more": False,
            "scanned": 0,
            "skipped": 0,
            "source_characters": 0,
            "network_enabled": False,
            "llm_calls": 0,
        }
        for row in db.execute(
            "SELECT rowid,* FROM evidence WHERE rowid>? AND kind='web' AND status='ok' "
            "ORDER BY rowid",
            (after,),
        ):
            if len(result["items"]) >= limit or result["scanned"] >= 1000:
                result["has_more"] = True
                break
            item = card(row) if (row["id"], row["digest"]) not in known else None
            if item:
                item["contacts"] = [
                    email
                    for email in item["contacts"]
                    if email not in recipients and email.rsplit("@", 1)[-1] not in companies
                ]
                if not item["contacts"] or urlsplit(item["url"]).hostname in companies:
                    item = None
            candidate = {
                **result,
                "items": result["items"] + ([item] if item else []),
                "next_cursor": row["rowid"],
                "scanned": result["scanned"] + 1,
                "skipped": result["skipped"] + int(item is None),
                "source_characters": result["source_characters"] + len(row["content"]),
            }
            if len(encode(candidate)) > max_chars:
                if not result["items"]:
                    raise ValueError("One evidence card exceeds the selected context budget")
                result["has_more"] = True
                break
            result = candidate
        return result
    finally:
        db.close()
