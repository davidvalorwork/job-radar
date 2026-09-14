"""Single-connection SQLite adapter with transactional business/audit writes."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from job_radar.domain.models import BatchResult, EvaluatedJob

MINIMUM_SQLITE = (3, 51, 3)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'failed'))
);
CREATE TABLE IF NOT EXISTS jobs (
    job_key TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('qualified', 'review', 'rejected')),
    profile_id TEXT,
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    reasons TEXT NOT NULL,
    last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS jobs_decision ON jobs(decision, profile_id);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    attributes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_run ON audit_events(run_id, sequence);
PRAGMA user_version = 1;
"""


class SQLiteJobRepository:
    def __init__(self, path: Path) -> None:
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
            raise RuntimeError("SQLite >= 3.51.3 is required for the WAL safety baseline")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported database schema version")
            self.connection.execute("PRAGMA foreign_keys = ON")
            mode = self.connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("WAL mode could not be enabled")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.connection.executescript(SCHEMA)
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _audit(self, run_id: str, event: str, at: datetime, attributes: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(event_id,run_id,event_type,occurred_at,attributes) "
            "VALUES (?,?,?,?,?)",
            (str(uuid4()), run_id, event, at.isoformat(), json.dumps(attributes, sort_keys=True)),
        )

    def begin_run(self, run_id: str, config_hash: str, at: datetime) -> None:
        with self._transaction():
            self.connection.execute(
                "INSERT INTO runs(run_id,config_hash,started_at,state) VALUES (?,?,?,'running')",
                (run_id, config_hash, at.isoformat()),
            )
            self._audit(run_id, "run.started", at, {"config_hash": config_hash})

    def save_batch(self, records: Sequence[EvaluatedJob], run_id: str, at: datetime) -> BatchResult:
        inserted = updated = unchanged = 0
        with self._transaction():
            active = self.connection.execute(
                "SELECT state FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if active is None or active[0] != "running":
                raise ValueError("Batch requires an active run")
            for record in records:
                job, evaluation = record.job, record.evaluation
                data = asdict(job)
                data["published_at"] = job.published_at.isoformat() if job.published_at else None
                payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
                decision_json = json.dumps(asdict(evaluation), sort_keys=True)
                record_hash = sha256(f"{payload}\n{decision_json}".encode()).hexdigest()
                previous = self.connection.execute(
                    "SELECT record_hash FROM jobs WHERE job_key=?",
                    (job.key,),
                ).fetchone()
                if previous is not None and previous[0] == record_hash:
                    unchanged += 1
                    outcome = "unchanged"
                else:
                    self.connection.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(job_key) DO UPDATE SET "
                        "payload=excluded.payload, record_hash=excluded.record_hash, "
                        "decision=excluded.decision,profile_id=excluded.profile_id, "
                        "score=excluded.score,reasons=excluded.reasons,last_run_id=excluded.last_run_id",
                        (
                            job.key,
                            job.url,
                            payload,
                            record_hash,
                            str(evaluation.decision),
                            evaluation.profile_id,
                            evaluation.score,
                            json.dumps(evaluation.reasons),
                            run_id,
                        ),
                    )
                    if previous is None:
                        inserted += 1
                        outcome = "inserted"
                    else:
                        updated += 1
                        outcome = "updated"
                self._audit(
                    run_id,
                    "job.evaluated",
                    at,
                    {
                        "job_key": job.key,
                        "source_id": job.source_id,
                        "outcome": outcome,
                        "record_hash": record_hash,
                        "decision": str(evaluation.decision),
                        "reasons": list(evaluation.reasons),
                    },
                )
        return BatchResult(inserted, updated, unchanged)

    def finish_run(self, run_id: str, state: str, at: datetime) -> None:
        if state not in {"completed", "failed"}:
            raise ValueError("Invalid terminal run state")
        with self._transaction():
            changed = self.connection.execute(
                "UPDATE runs SET state=?,finished_at=? WHERE run_id=? AND state='running'",
                (state, at.isoformat(), run_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Run is not active")
            self._audit(run_id, f"run.{state}", at, {})


def read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"initialized": False, "jobs": 0, "decisions": {}, "runs": {}, "audit_events": 0}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("Unsupported database schema version")
        return {
            "initialized": True,
            "jobs": connection.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "decisions": dict(
                connection.execute("SELECT decision,count(*) FROM jobs GROUP BY decision")
            ),
            "runs": dict(connection.execute("SELECT state,count(*) FROM runs GROUP BY state")),
            "audit_events": connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
        }
    finally:
        connection.close()
