"""Outbound contracts owned by the application layer."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from job_radar.domain.models import BatchResult, EvaluatedJob, Job


class Clock(Protocol):
    def now(self) -> datetime: ...


class JobReader(Protocol):
    def read(self) -> Iterable[Job]: ...


class JobRepository(Protocol):
    def begin_run(self, run_id: str, config_hash: str, at: datetime) -> None: ...

    def save_batch(
        self, records: Sequence[EvaluatedJob], run_id: str, at: datetime
    ) -> BatchResult: ...

    def finish_run(self, run_id: str, state: str, at: datetime) -> None: ...


class Telemetry(Protocol):
    def event(self, name: str, fields: dict[str, str | int]) -> None: ...
