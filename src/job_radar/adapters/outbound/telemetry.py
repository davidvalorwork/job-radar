"""Local JSON logs and bounded-label metric snapshots; no remote exports."""

import sys
from contextlib import suppress
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Gauge, generate_latest


class JsonTelemetry:
    def __init__(self) -> None:
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )
        self.logger = structlog.get_logger()

    def event(self, name: str, fields: dict[str, str | int]) -> None:
        # The use case only provides event IDs and counters, not job descriptions or credentials.
        # Durable audit already committed; diagnostics must not invalidate it.
        with suppress(OSError):
            self.logger.info(name, **fields)


def metrics_snapshot(status: dict[str, Any]) -> str:
    registry = CollectorRegistry()
    jobs = Gauge("radar_jobs", "Current persisted jobs", ["decision"], registry=registry)
    for decision in ("qualified", "review", "rejected"):
        jobs.labels(decision=decision).set(status["decisions"].get(decision, 0))
    audit = Gauge("radar_audit_events", "Committed audit events", registry=registry)
    audit.set(status["audit_events"])
    llm = Gauge("radar_cloud_llm_enabled", "Cloud LLM functionality enabled", registry=registry)
    llm.set(0)
    return generate_latest(registry).decode()
