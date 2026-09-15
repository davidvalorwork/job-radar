import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest

from job_radar.adapters.outbound.sqlite import SQLiteJobRepository, read_status
from job_radar.bootstrap import main
from job_radar.domain.models import Decision, EvaluatedJob, Evaluation, FilterPolicy
from job_radar.domain.qualification import evaluate


def test_two_independent_writers_do_not_duplicate_jobs(tmp_path, now, job):
    path = tmp_path / "radar.sqlite3"
    SQLiteJobRepository(path).close()
    record = EvaluatedJob(job, Evaluation(Decision.QUALIFIED, "devops", 50, ()))

    def write(run_id):
        repository = SQLiteJobRepository(path)
        try:
            repository.begin_run(run_id, "hash", now)
            result = repository.save_batch([record], run_id, now)
            repository.finish_run(run_id, "completed", now)
            return result
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["worker-1", "worker-2"]))
    assert sum(result.inserted for result in results) == 1
    assert sum(result.unchanged for result in results) == 1
    assert read_status(path)["audit_events"] == 6


def test_terminal_audit_failure_keeps_run_active(tmp_path, now):
    repository = SQLiteJobRepository(tmp_path / "radar.sqlite3")
    try:
        repository.begin_run("run", "hash", now)
        repository.connection.execute("""
            CREATE TRIGGER reject_terminal_event BEFORE INSERT ON audit_events
            WHEN NEW.event_type = 'run.completed'
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
        """)
        with pytest.raises(sqlite3.IntegrityError):
            repository.finish_run("run", "completed", now)
        row = repository.connection.execute("SELECT state,finished_at FROM runs").fetchone()
        assert tuple(row) == ("running", None)
    finally:
        repository.close()


def test_cli_storage_error_does_not_leak_path(tmp_path, capsys):
    with closing(sqlite3.connect(tmp_path / "radar.sqlite3")) as connection:
        connection.execute("PRAGMA user_version=999")
    assert main(["status", "--data-dir", str(tmp_path)]) == 4
    result = capsys.readouterr().out
    assert json.loads(result)["error"] == "storage_or_runtime_error"
    assert str(tmp_path) not in result


def test_exact_age_boundary_and_rejection_precedence(job, profiles, now):
    on_boundary = replace(job, published_at=now - timedelta(days=30))
    assert evaluate(on_boundary, profiles, FilterPolicy(), now).decision == Decision.QUALIFIED
    beyond = replace(
        on_boundary, published_at=on_boundary.published_at - timedelta(microseconds=1), remote=None
    )
    result = evaluate(beyond, profiles, FilterPolicy(), now)
    assert result.decision == Decision.REJECTED
    assert "publication_too_old" in result.reasons and "remote_unknown" in result.reasons
