"""Conservative evidence checks, not contact authorization or a salary parser."""

import re

from job_radar.domain.models import Job

ORIGINAL = re.compile(r"^#{1,6}\s+Original posting\s*$", re.IGNORECASE | re.MULTILINE)
SECTION = re.compile(r"^#{1,6}\s+", re.MULTILINE)
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE)
NO_CURRENT_CASH = re.compile(
    r"\b(?<!not )equity[- ]only\b|\bno cash (?:salary|compensation)\b", re.IGNORECASE
)
CASH_RATE = re.compile(
    r"(?:USD\s*|\$\s*)\d[\d,.]*\s*k?\s*"
    r"(?:(?:[-–—]|to)\s*(?:USD\s*|\$\s*)?\d[\d,.]*\s*k?)?\s*"
    r"(?:/\s*|per\s+|a\s+)(?:yr|year|annum|mo|month|hr|hour)\b",
    re.IGNORECASE,
)


def original_posting(text: str) -> str | None:
    """Ignore an aggregator's generated table and subsequent related-posting sections.

    This is still cached third-party text, not independently verified primary evidence.
    A missing section is different from an empty one; neither certifies a contact.
    """
    heading = ORIGINAL.search(text)
    if heading is None:
        return None
    rest = text[heading.end() :]
    following = SECTION.search(rest)
    return rest[: following.start()] if following else rest


def unsupported_aggregator_contact(text: str, recipient: str) -> bool:
    original = original_posting(text)
    return original is not None and recipient.strip().casefold() not in {
        match.group().rstrip(".").casefold() for match in EMAIL.finditer(original)
    }


def salary_evidence_requires_review(job: Job) -> bool:
    original = original_posting(job.description)
    text = job.description if original is None else original
    if NO_CURRENT_CASH.search(text):
        return True
    unstructured = (
        not job.salary_disclosed
        and job.salary_min_monthly_usd is None
        and job.salary_max_monthly_usd is None
    )
    return unstructured and CASH_RATE.search(text) is not None
