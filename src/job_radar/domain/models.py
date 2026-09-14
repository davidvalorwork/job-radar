"""Immutable, infrastructure-independent job discovery concepts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(value: str) -> str:
    """Normalize identity without discarding potentially meaningful query parameters.

    This is identity validation, NOT an SSRF-safe network client or access authorization.
    """
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("An absolute HTTP(S) URL is required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are forbidden")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid port")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    query = sorted(
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
    )
    return urlunsplit((parts.scheme, authority, parts.path or "/", urlencode(query), ""))


class Decision(StrEnum):
    QUALIFIED = "qualified"
    REVIEW = "review"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Job:
    source_id: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    remote: bool | None = None
    published_at: datetime | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.source_id.strip(),
                self.external_id.strip(),
                self.title.strip(),
                self.company.strip(),
            )
        ):
            raise ValueError("Job identifiers, title and company must be non-empty")
        object.__setattr__(self, "url", canonical_url(self.url))
        if self.published_at is not None and self.published_at.utcoffset() is None:
            raise ValueError("published_at must include a timezone")

    @property
    def key(self) -> str:
        return sha256(self.url.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RoleProfile:
    id: str
    priority: int
    title_terms: tuple[str, ...]
    preferred_terms: tuple[str, ...] = ()
    required_context_any: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    remote_only: bool = True
    max_age_days: int = 30
    languages: tuple[str, ...] = ("en", "es")
    excluded_companies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    decision: Decision
    profile_id: str | None
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluatedJob:
    job: Job
    evaluation: Evaluation


@dataclass(frozen=True, slots=True)
class BatchResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class ImportResult:
    run_id: str
    records: int
    inserted: int
    updated: int
    unchanged: int
