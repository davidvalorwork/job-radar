"""HN-specific source provenance and review signals. Never grants contact approval."""

import re
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlsplit

from job_radar.domain.evidence import EMAIL, original_posting

ROLE = re.compile(r"engineer|developer|devops|\bsre\b|full.?stack|backend|frontend", re.I)
SIGNALS = {
    "application_instructions": re.compile(
        r"\bsubject\b|\basunto\b|(?:do not|don't|don’t) use AI|anti[- ]bot|"
        r"cover letter|include.{0,30}(?:story|example|portfolio)",
        re.I,
    ),
    "geographic_eligibility": re.compile(
        r"(?:US|U\.S\.|USA|EU|UK|Europe|Canada|India).{0,25}(?:only|citizens|based)|"
        r"(?:must|need to) (?:be |live |reside ).{0,40}|security clearance|"
        r"onsite|on-site|hybrid",
        re.I,
    ),
    "posting_closed": re.compile(
        r"(?:position|role|job|opening) (?:is |has been )?(?:closed|filled)|"
        r"no longer (?:hiring|accepting)",
        re.I,
    ),
}


def inspect_posting(url: str, content: str, recipient: str = "") -> dict[str, Any] | None:
    parsed = urlsplit(url)
    aggregator = parsed.hostname == "hnhiring.fyi" and bool(re.fullmatch(r"/job/\d+", parsed.path))
    hn = (
        parsed.hostname == "news.ycombinator.com"
        and parsed.path == "/item"
        and bool(re.fullmatch(r"\d+", parse_qs(parsed.query).get("id", [""])[0]))
    )
    if not (aggregator or hn) or parsed.scheme != "https":
        return None
    match = re.search(r"https://news\.ycombinator\.com/item\?id=\d+", content)
    primary = match[0] if aggregator and match else url if hn else None
    original = (
        original_posting(content) if aggregator else content.split("Markdown Content:", 1)[-1]
    )
    body = (original or "").strip()
    header = next((line.strip("# ") for line in body.splitlines() if line.strip()), "")
    pieces = [part.strip() for part in header.split("|")]
    title = next((part for part in pieces[1:] if ROLE.search(part)), "")[:200]
    contacts = sorted({m.group().rstrip(".").casefold() for m in EMAIL.finditer(body)})
    flags = [name for name, pattern in SIGNALS.items() if pattern.search(body)]
    if not body or "not yet fully loaded" in content or "Markdown Content:" not in content and hn:
        flags.append("source_incomplete")
    if not title:
        flags.append("role_header_unknown")
    if primary is None:
        flags.append("primary_source_missing")
    present = bool(recipient) and recipient.casefold() in contacts
    if recipient and not present:
        flags.append("recipient_not_in_original")
    return {
        "primary_url": primary,
        "source_digest": sha256(content.encode()).hexdigest(),
        "title": title,
        "company": pieces[0][:256] if pieces else "",
        "body": body,
        "contacts": contacts,
        "contact_present": present,
        "flags": flags,
        "requires_review": True,
    }
