"""Composition root: the sole place where application and concrete adapters meet."""

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil
import yaml

from job_radar import __version__
from job_radar.adapters.inbound.cli import parser
from job_radar.adapters.outbound.clock import SystemClock
from job_radar.adapters.outbound.config import load_bundle
from job_radar.adapters.outbound.external_receipts import external_plan
from job_radar.adapters.outbound.gmail_bridge import FileConnector, GmailBridge
from job_radar.adapters.outbound.gmail_native import authorize, connect, inspect_credentials
from job_radar.adapters.outbound.job_normalizer import normalized_jobs
from job_radar.adapters.outbound.jsonl import JsonlJobReader
from job_radar.adapters.outbound.mail_audit import audit_ledger
from job_radar.adapters.outbound.mail_plan import prepare_plan
from job_radar.adapters.outbound.mail_templates import compose
from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.adapters.outbound.research import (
    AgentReachReader,
    EvidenceCache,
    job_requests,
    read_requests,
)
from job_radar.adapters.outbound.review import encode, review_packet
from job_radar.adapters.outbound.sqlite import MINIMUM_SQLITE, SQLiteJobRepository, read_status
from job_radar.adapters.outbound.telemetry import JsonTelemetry, metrics_snapshot
from job_radar.application.collection import CollectBatch
from job_radar.application.delivery import DeliverBatch, DraftBatch
from job_radar.application.ingest import IngestJobs
from job_radar.application.planning import SearchSource, plan_queries
from job_radar.domain.mail import MAX_DAILY_SEND_LIMIT, DeliveryBlocked, DeliveryUnknown


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def mail_command(args: argparse.Namespace) -> int:
    if args.mail_command == "reconcile-external":
        clock = SystemClock()
        fingerprint, account, rows = external_plan(args.input, clock.now())
        if args.confirm is None:
            emit(
                {
                    "confirmation": fingerprint,
                    "verified_export_records": len(rows),
                    "sending_enabled": False,
                    "imported": 0,
                }
            )
            return 0
        if args.confirm != fingerprint:
            raise ValueError("Exact external reconciliation confirmation required")
        ledger = SQLiteOutbox(args.data_dir / "outbox.sqlite3")
        try:
            imported = ledger.reconcile_external(account, rows, fingerprint, clock.now())
            emit(
                {
                    "imported": imported,
                    "already_present": len(rows) - imported,
                    "sending_enabled": False,
                }
            )
        finally:
            ledger.close()
        return 0
    if args.mail_command == "audit":
        report = audit_ledger(args.input, args.data_dir / "outbox.sqlite3")
        emit(report)
        return 0 if report["ledger_complete"] else 7
    if args.mail_command == "check":
        readiness = inspect_credentials(args.credentials)
        emit(readiness)
        return 0 if readiness["local_credentials_configured"] else 6
    if args.mail_command == "compose":
        emit(compose(args.input, args.profile, args.output))
        return 0
    if args.mail_command == "auth":
        authorize(args.client_secret, args.credentials, args.account)
        emit({"authorized": True, "sending_enabled": False})
        return 0
    path = args.data_dir / "outbox.sqlite3"
    if args.mail_command == "status" and not path.exists():
        emit({"initialized": False, "states": {}})
        return 0
    outbox = SQLiteOutbox(path)
    clock = SystemClock()
    native = None
    try:
        if args.mail_command == "prepare":
            batch, mails, counts, manifest = prepare_plan(args.manifest, args.config)
            if mails:
                outbox.prepare(
                    batch,
                    mails,
                    clock.now(),
                    manifest.suppressed_recipients,
                    manifest.suppressed_companies,
                )
            emit({"batch": batch, "counts": counts, "sending_enabled": False})
        elif args.mail_command == "status":
            emit(outbox.status())
        elif args.mail_command == "hold":
            if args.confirm != args.batch:
                raise ValueError("Exact batch confirmation required")
            emit({"held": outbox.hold_batch(args.batch, clock.now()), "sending_enabled": False})
        elif args.mail_command == "run":
            if args.confirm != args.batch or not 1 <= args.daily_limit <= MAX_DAILY_SEND_LIMIT:
                raise ValueError("Invalid authorization or quota")
            native = connect(args.credentials) if args.credentials else None
            gateway = GmailBridge(
                native if native else FileConnector(args.bridge_dir, timeout=args.bridge_timeout)
            )
            result = DeliverBatch(outbox, gateway, clock, JsonTelemetry()).execute(
                args.batch, confirmed=args.confirm, daily_limit=args.daily_limit
            )
            emit(result)
            if native:
                emit({"transport": "gmail_oauth", **native.metrics})
            if result["unknown"]:
                return 5
        elif args.mail_command == "draft":
            if args.confirm != args.batch:
                raise ValueError("Invalid draft confirmation")
            gateway = GmailBridge(FileConnector(args.bridge_dir, timeout=args.bridge_timeout))
            result = DraftBatch(outbox, gateway, clock, JsonTelemetry()).execute(
                args.batch, confirmed=args.confirm
            )
            emit(result)
            if result["unknown"]:
                return 5
        elif args.mail_command == "reconcile":
            mail, stored_id = outbox.get(args.batch, args.mail_id)
            message_id = args.message_id or stored_id
            if not message_id:
                raise ValueError("Find the actual Gmail message ID before reconciliation")
            native = connect(args.credentials) if args.credentials else None
            gateway = GmailBridge(
                native if native else FileConnector(args.bridge_dir, timeout=args.bridge_timeout)
            )
            if gateway.profile() != mail.account:
                raise DeliveryBlocked("account_mismatch")
            receipt = gateway.verify(mail, message_id)
            outbox.finish(args.batch, mail, receipt, clock.now())
            emit({"reconciled": True, "message_id": receipt.id})
        return 0
    finally:
        if native:
            native.close()
        outbox.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "mail":
            return mail_command(args)
        if args.command == "review":
            packet = review_packet(
                args.data_dir / "evidence.sqlite3",
                limit=args.limit,
                max_chars=args.max_chars,
                after=args.after,
                snapshot=args.snapshot,
                seen=tuple(args.seen),
                outbox=args.outbox,
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as target:
                    target.write(encode(packet))
                emit(
                    {
                        "saved": True,
                        "items": len(packet["items"]),
                        "next_cursor": packet["next_cursor"],
                        "snapshot": packet["snapshot"],
                        "has_more": packet["has_more"],
                        "llm_calls": 0,
                    }
                )
            else:
                print(encode(packet))
            return 0
        if args.command == "collect":
            requests = read_requests(args.input)
            cache = EvidenceCache(args.data_dir / "evidence.sqlite3")
            try:
                emit(
                    CollectBatch(AgentReachReader(), cache).execute(
                        requests, workers=args.workers, ttl=args.ttl_seconds
                    )
                )
            finally:
                cache.close()
            return 0
        if args.command == "expand":
            requests = job_requests(
                args.data_dir / "evidence.sqlite3",
                limit=args.limit,
                after=args.after,
                unseen_only=args.unseen_only,
            )
            cache = EvidenceCache(args.data_dir / "evidence.sqlite3")
            try:
                collection_result = CollectBatch(AgentReachReader(), cache).execute(
                    requests, workers=args.workers, ttl=args.ttl_seconds
                )
                emit({"after": args.after, "discovered": len(requests), **collection_result})
            finally:
                cache.close()
            return 0
        if args.command == "normalize":
            records = list(
                normalized_jobs(
                    args.data_dir / "evidence.sqlite3", limit=args.limit, after=args.after
                )
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                for record in records:
                    encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
                    output.write(encoded + "\n")
            emit({"records": len(records), "llm_calls": 0, "saved": True})
            return 0
        if args.command == "doctor":
            runtime_ok = (
                sys.version_info[:2] == (3, 14) and sqlite3.sqlite_version_info >= MINIMUM_SQLITE
            )
            memory = psutil.virtual_memory()
            emit(
                {
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "sqlite": sqlite3.sqlite_version,
                    "runtime_ok": runtime_ok,
                    "resources": {
                        "logical_cpus": psutil.cpu_count(),
                        "available_memory_mib": memory.available // (1024 * 1024),
                    },
                    "optional_tools": {
                        name: shutil.which(name) is not None
                        for name in ("opencli", "agent-reach", "codegraph")
                    },
                    "session_status": "not_checked",
                    "network_enabled": False,
                    "sending_enabled": False,
                    "mail_transport": "opt_in_gmail_oauth_or_assisted_bridge",
                }
            )
            return 0 if runtime_ok else 1
        if args.command in {"init", "status", "metrics"}:
            path: Path = args.data_dir / "radar.sqlite3"
            if args.command == "init":
                repository = SQLiteJobRepository(path)
                repository.close()
            status = read_status(path)
            if args.command == "metrics":
                print(metrics_snapshot(status), end="")
            else:
                emit(status)
            return 0

        config, catalog, config_hash = load_bundle(args.config)
        if args.command == "config":
            emit(
                {
                    "valid": True,
                    "config_hash": config_hash,
                    "sources": len(catalog.sources),
                    "profiles": [profile.id for profile in config.profiles],
                }
            )
        elif args.command in {"catalog", "plan"}:
            if set(args.source) - {source.id for source in catalog.sources}:
                raise ValueError("Unknown source selection")
            sources = [
                source
                for source in catalog.sources
                if (not args.category or source.category in args.category)
                and (not args.source or source.id in args.source)
            ]
            if args.command == "catalog":
                emit(
                    [
                        source.model_dump(mode="json")
                        for source in sources
                        if not args.enabled_only or source.enabled
                    ]
                )
            else:
                limit = config.runtime.max_queries if args.limit is None else args.limit
                if not 1 <= limit <= config.runtime.max_queries:
                    raise ValueError("Plan limit exceeds configured budget")
                plan = plan_queries(
                    [
                        SearchSource(
                            source.id,
                            source.category,
                            source.priority,
                            source.url.host
                            if source.query_support == "domain" and source.url
                            else None,
                        )
                        for source in sources
                        if source.enabled
                    ],
                    [
                        (profile.id, profile.queries)
                        for profile in sorted(
                            config.profiles, key=lambda profile: (-profile.priority, profile.id)
                        )
                    ],
                    limit,
                )
                planned = {
                    "executed": False,
                    "config_hash": config_hash,
                    "count": len(plan),
                    "queries": [asdict(query) for query in plan],
                }
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open("x", encoding="utf-8") as output:
                        json.dump(planned, output, ensure_ascii=True)
                    emit({"executed": False, "count": len(plan), "saved": True})
                else:
                    emit(planned)
        elif args.command == "ingest":
            repository = SQLiteJobRepository(args.data_dir / "radar.sqlite3")
            try:
                use_case = IngestJobs(
                    repository,
                    SystemClock(),
                    JsonTelemetry(),
                    tuple(profile.domain() for profile in config.profiles),
                    config.filters.domain(),
                    config_hash,
                    config.runtime.batch_size,
                    config.runtime.max_records,
                )
                result = use_case.execute(
                    JsonlJobReader(args.input, {s.id for s in catalog.sources})
                )
                emit(asdict(result))
            finally:
                repository.close()
        return 0
    except ValueError, TypeError, KeyError, yaml.YAMLError, RecursionError:
        # Never print validation payloads: imported data could contain personal information.
        emit(
            {
                "error": "invalid_input",
                "message": "Check configuration, selections and input schema.",
            }
        )
        return 2
    except OSError:
        emit(
            {"error": "io_error", "message": "Check local file permissions and available storage."}
        )
        return 3
    except DeliveryBlocked as error:
        emit({"error": "delivery_blocked", "reason": str(error)})
        return 6
    except DeliveryUnknown:
        emit({"error": "delivery_unknown", "message": "Reconcile before retrying."})
        return 5
    except sqlite3.Error, RuntimeError:
        emit(
            {
                "error": "storage_or_runtime_error",
                "message": "Check radar doctor and database schema.",
            }
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
