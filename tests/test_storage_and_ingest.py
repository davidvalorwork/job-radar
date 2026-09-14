import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from job_radar.adapters.outbound.jsonl import JsonlJobReader
from job_radar.adapters.outbound.sqlite import SQLiteJobRepository, read_status
from job_radar.application.ingest import IngestJobs
from job_radar.domain.models import Decision, EvaluatedJob, Evaluation, FilterPolicy


class FixedClock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


class MemoryTelemetry:
    def __init__(self):
        self.events = []

    def event(self, name, fields):
        self.events.append((name, fields))


class Reader:
    def __init__(self, jobs):
        self.jobs = jobs

    def read(self):
        yield from self.jobs


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteJobRepository(tmp_path / "radar.sqlite3")
    yield repo
    repo.close()


def test_idempotent_import_and_audit(repository, tmp_path, now, profiles, job):
    telemetry = MemoryTelemetry()
    use_case = IngestJobs(
        repository, FixedClock(now), telemetry, profiles, FilterPolicy(), "abc", 1
    )
    assert use_case.execute(Reader([job])).inserted == 1
    assert use_case.execute(Reader([job])).unchanged == 1
    assert use_case.execute(Reader([replace(job, description="Updated terraform")])).updated == 1
    status = read_status(tmp_path / "radar.sqlite3")
    assert status["jobs"] == 1
    assert status["audit_events"] == 9
    assert status["runs"] == {"completed": 3}
    assert len(telemetry.events) == 3
    assert "Example Company" not in json.dumps(telemetry.events)


def test_business_and_audit_rollback_together(repository, now, job):
    repository.begin_run("run-1", "hash", now)
    repository.connection.execute("""
        CREATE TRIGGER simulate_audit_failure BEFORE INSERT ON audit_events
        WHEN NEW.event_type = 'job.evaluated'
        BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError):
        repository.save_batch(
            [EvaluatedJob(job, Evaluation(Decision.QUALIFIED, "devops", 60, ()))], "run-1", now
        )
    assert repository.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert repository.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1


def test_failed_input_retains_completed_batches(repository, tmp_path, now, profiles, job):
    class BrokenReader:
        def read(self):
            yield job
            raise ValueError("broken line")

    use_case = IngestJobs(
        repository,
        FixedClock(now),
        MemoryTelemetry(),
        profiles,
        FilterPolicy(),
        "hash",
        batch_size=1,
    )
    with pytest.raises(ValueError):
        use_case.execute(BrokenReader())
    status = read_status(tmp_path / "radar.sqlite3")
    assert status["jobs"] == 1
    assert status["runs"] == {"failed": 1}
    assert status["audit_events"] == 3


def test_input_budget(repository, now, profiles, job):
    use_case = IngestJobs(
        repository,
        FixedClock(now),
        MemoryTelemetry(),
        profiles,
        FilterPolicy(),
        "hash",
        batch_size=100,
        max_records=1,
    )
    with pytest.raises(ValueError, match="max_records"):
        use_case.execute(Reader([job, job]))
    assert repository.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_invalid_run_transitions(repository, now):
    with pytest.raises(ValueError):
        repository.save_batch([], "missing", now)
    repository.begin_run("run", "hash", now)
    with pytest.raises(ValueError):
        repository.finish_run("run", "unknown", now)
    repository.finish_run("run", "completed", now)
    with pytest.raises(ValueError):
        repository.finish_run("run", "completed", now)


def test_read_status_does_not_initialize(tmp_path):
    path = tmp_path / "absent" / "radar.sqlite3"
    assert read_status(path)["initialized"] is False
    assert not path.parent.exists()


def test_schema_version_guard(tmp_path):
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="schema"):
        SQLiteJobRepository(path)
    with pytest.raises(RuntimeError, match="schema"):
        read_status(path)


def test_sqlite_safety_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 50, 4))
    path = tmp_path / "not-created" / "radar.sqlite3"
    with pytest.raises(RuntimeError, match="WAL"):
        SQLiteJobRepository(path)
    assert not path.parent.exists()


def test_jsonl_reader(tmp_path, job):
    path = tmp_path / "jobs.jsonl"
    data = asdict(job)
    data["published_at"] = job.published_at.isoformat()
    path.write_text("\n" + json.dumps(data) + "\n", encoding="utf-8")
    assert list(JsonlJobReader(path, {job.source_id}).read()) == [job]
    with pytest.raises(ValueError, match="line 2"):
        list(JsonlJobReader(path, {"unknown"}).read())


@pytest.mark.parametrize(
    "payload",
    ['{"secret":"do-not-log-this"}', "x" * 65_537, '{"remote":"true"}', "{invalid json}"],
    ids=["unknown-field", "oversized", "string-bool", "invalid-json"],
)
def test_invalid_jsonl_does_not_echo_payload(tmp_path, payload):
    path = tmp_path / "invalid.jsonl"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        list(JsonlJobReader(path, {"remoteok"}).read())
    assert "do-not-log-this" not in str(error.value)


def test_total_jsonl_limit(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_bytes(b" \n" * 4_194_305)
    with pytest.raises(ValueError, match="size limit"):
        list(JsonlJobReader(path, {"remoteok"}).read())
