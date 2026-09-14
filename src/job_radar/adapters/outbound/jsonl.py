"""Bounded local JSONL input; never fetches URLs or expands external references."""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from job_radar.domain.models import Job


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: Annotated[str, Field(min_length=1, max_length=64)]
    external_id: Annotated[str, Field(min_length=1, max_length=256)]
    title: Annotated[str, Field(min_length=1, max_length=512)]
    company: Annotated[str, Field(min_length=1, max_length=256)]
    url: Annotated[str, Field(min_length=1, max_length=4096)]
    description: Annotated[str, Field(max_length=50000)] = ""
    remote: bool | None = None
    published_at: AwareDatetime | None = None
    language: Annotated[str, Field(min_length=2, max_length=16)] | None = None


class JsonlJobReader:
    def __init__(self, path: Path, allowed_sources: set[str]) -> None:
        self.path = path
        self.allowed_sources = allowed_sources

    def read(self) -> Iterator[Job]:
        total_bytes = 0
        with self.path.open("rb") as stream:
            line_number = 0
            while line := stream.readline(65537):
                line_number += 1
                total_bytes += len(line)
                if len(line) > 65536 or total_bytes > 8_388_608:
                    raise ValueError("Input exceeds the line or file size limit")
                if not line.strip():
                    continue
                try:
                    record = JobRecord.model_validate_json(line)
                    if record.source_id not in self.allowed_sources:
                        raise ValueError("Source is not registered")
                    yield Job(**record.model_dump())
                except (ValidationError, ValueError) as error:
                    raise ValueError(f"Invalid job record at line {line_number}") from error
