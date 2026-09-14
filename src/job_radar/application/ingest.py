"""Offline ingestion with bounded batches and durable per-batch audit."""

from uuid import uuid4

from job_radar.application.ports import Clock, JobReader, JobRepository, Telemetry
from job_radar.domain.models import EvaluatedJob, FilterPolicy, ImportResult, RoleProfile
from job_radar.domain.qualification import evaluate


class IngestJobs:
    def __init__(
        self,
        repository: JobRepository,
        clock: Clock,
        telemetry: Telemetry,
        profiles: tuple[RoleProfile, ...],
        policy: FilterPolicy,
        config_hash: str,
        batch_size: int = 100,
        max_records: int = 2000,
    ) -> None:
        if not 1 <= batch_size <= 1000 or max_records < 1:
            raise ValueError("Invalid ingestion limits")
        self.repository = repository
        self.clock = clock
        self.telemetry = telemetry
        self.profiles = profiles
        self.policy = policy
        self.config_hash = config_hash
        self.batch_size = batch_size
        self.max_records = max_records

    def execute(self, reader: JobReader) -> ImportResult:
        run_id = str(uuid4())
        at = self.clock.now()
        self.repository.begin_run(run_id, self.config_hash, at)
        pending: list[EvaluatedJob] = []
        count = inserted = updated = unchanged = 0
        try:
            for job in reader.read():
                count += 1
                if count > self.max_records:
                    raise ValueError("Input exceeds configured max_records")
                pending.append(EvaluatedJob(job, evaluate(job, self.profiles, self.policy, at)))
                if len(pending) >= self.batch_size:
                    result = self.repository.save_batch(pending, run_id, at)
                    inserted += result.inserted
                    updated += result.updated
                    unchanged += result.unchanged
                    pending.clear()
            if pending:
                result = self.repository.save_batch(pending, run_id, at)
                inserted += result.inserted
                updated += result.updated
                unchanged += result.unchanged
            self.repository.finish_run(run_id, "completed", self.clock.now())
        except Exception:
            # Previously committed batches remain valid; the run is not reported complete.
            self.repository.finish_run(run_id, "failed", self.clock.now())
            raise
        result_value = ImportResult(run_id, count, inserted, updated, unchanged)
        self.telemetry.event(
            "ingest.completed",
            {
                "run_id": run_id,
                "records": count,
                "inserted": inserted,
                "updated": updated,
                "unchanged": unchanged,
            },
        )
        return result_value
