"""Deterministic, local normalization of cached public job pages."""

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from job_radar.adapters.outbound.posting_evidence import inspect_posting

TITLE = re.compile(r"(?m)^Title:\s*(.+)$")
HEADING = re.compile(r"(?m)^#{1,3}\s+(.+)$")
COMPANY = re.compile(r"(?im)^(?:company|empresa)\s*:\s*(.+)$")
TITLE_COMPANY = re.compile(r"\s+(?:at|en)\s+(.+)$", re.I)
REMOTE = re.compile(r"\bremote\b|remot[oa]|worldwide|anywhere", re.I)
SOURCE_IDS = {
    "remoteok.com": "remoteok",
    "weworkremotely.com": "weworkremotely",
    "remotive.com": "remotive",
    "getonbrd.com": "getonbrd",
    "wellfound.com": "wellfound",
}
DESCRIPTION_LIMIT = 8_000


def source_id(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return next(
        (
            value
            for suffix, value in SOURCE_IDS.items()
            if host == suffix or host.endswith("." + suffix)
        ),
        "company_pages",
    )


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return " ".join(match[1].split())[:512] if match else None


def _title(text: str) -> str | None:
    return _first(TITLE, text) or _first(HEADING, text)


def _company(title: str, text: str, url: str) -> str:
    explicit = _first(COMPANY, text)
    if explicit:
        return explicit[:256]
    match = TITLE_COMPANY.search(title)
    if match:
        return " ".join(match[1].split())[:256]
    return (urlsplit(url).hostname or "unknown-employer").removeprefix("www.")[:256]


def _description(text: str) -> str:
    body = text.split("Markdown Content:", 1)[-1]
    # Keep a full normalized batch under the JSONL reader's 8 MiB input
    # ceiling while retaining enough role and compensation context.
    return body[:DESCRIPTION_LIMIT]


def normalized_jobs(path: Path, *, limit: int, after: int = 0) -> Iterable[dict[str, object]]:
    """Yield public job records without creating contacts or guessing salary data."""
    if not 1 <= limit <= 2000 or after < 0:
        raise ValueError("Invalid normalization budget")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        emitted = skipped = 0
        for url, content in connection.execute(
            "SELECT value,content FROM evidence WHERE kind='web' AND status='ok' ORDER BY at"
        ):
            hn = inspect_posting(url, content)
            if hn is not None and (
                not hn["title"] or not hn["primary_url"] or "source_incomplete" in hn["flags"]
            ):
                continue
            title = hn["title"] if hn else _title(content)
            if title is None:
                continue
            if skipped < after:
                skipped += 1
                continue
            description = str(hn["body"])[:DESCRIPTION_LIMIT] if hn else _description(content)
            record_url = str(hn["primary_url"]) if hn else url
            yield {
                "source_id": "hackernews" if hn else source_id(url),
                "external_id": hashlib.sha256(record_url.encode()).hexdigest()[:32],
                "title": title,
                "company": hn["company"] if hn else _company(title, description, url),
                "url": record_url,
                "description": description,
                "remote": bool(REMOTE.search(f"{title}\n{description}")),
                # Salary text can be annual, hourly or locale-specific.  Keep
                # it as disclosed-but-unconverted so the policy requests review
                # rather than inventing a monthly USD conversion.
                "salary_disclosed": bool(re.search(r"\$|USD|salary|salario", description, re.I)),
            }
            emitted += 1
            if emitted == limit:
                return
    finally:
        connection.close()
