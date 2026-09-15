"""Bounded IO batches and exact-request deduplication; never invokes an LLM."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ResearchRequest:
    kind: str
    value: str


@dataclass(frozen=True)
class ResearchResponse:
    status: str
    content: str


class Reader(Protocol):
    def fetch(self, request: ResearchRequest) -> ResearchResponse: ...


class ResearchCache(Protocol):
    def fresh(self, request: ResearchRequest, ttl: int) -> bool: ...

    def put(self, request: ResearchRequest, response: ResearchResponse) -> None: ...


class CollectBatch:
    def __init__(self, reader: Reader, cache: ResearchCache) -> None:
        self.reader, self.cache = reader, cache

    def execute(
        self, requests: Sequence[ResearchRequest], *, workers: int = 4, ttl: int = 86400
    ) -> dict[str, int]:
        if not 1 <= workers <= 8 or not 0 <= ttl <= 604800 or len(requests) > 1000:
            raise ValueError("Invalid collection budget")
        unique = list(dict.fromkeys(requests))
        pending = [request for request in unique if not self.cache.fresh(request, ttl)]
        result = {
            "input": len(requests),
            "unique": len(unique),
            "cached": len(unique) - len(pending),
            "fetched": 0,
            "failed": 0,
            "llm_calls": 0,
        }

        def safe_fetch(request: ResearchRequest) -> ResearchResponse:
            try:
                return self.reader.fetch(request)
            except Exception:
                return ResearchResponse("reader_error", "")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for request, response in zip(pending, pool.map(safe_fetch, pending), strict=True):
                # One writer: workers never share a SQLite connection or mutate business state.
                self.cache.put(request, response)
                result["fetched" if response.status == "ok" else "failed"] += 1
        return result
