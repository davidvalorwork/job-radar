"""Bounded, deterministic query planning without calling websites or LLMs."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchSource:
    id: str
    category: str
    priority: int
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    source_id: str
    profile_id: str
    query: str


def plan_queries(
    sources: Sequence[SearchSource],
    profiles: Sequence[tuple[str, Sequence[str]]],
    limit: int,
) -> tuple[SearchQuery, ...]:
    if not 1 <= limit <= 1000:
        raise ValueError("Query limit must be between 1 and 1000")
    ordered = sorted(sources, key=lambda source: (-source.priority, source.id))
    seen: set[tuple[str, str]] = set()
    result: list[SearchQuery] = []
    # Interleave sources for each variant so a single source cannot consume the budget.
    for profile_id, variants in profiles:
        for query in variants:
            for source in ordered:
                scoped = f"site:{source.domain} {query.strip()}" if source.domain else query.strip()
                key = (source.id, scoped.casefold())
                if key in seen:
                    continue
                seen.add(key)
                result.append(SearchQuery(source.id, profile_id, scoped))
                if len(result) == limit:
                    return tuple(result)
    return tuple(result)
