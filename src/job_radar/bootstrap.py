"""Composition root: the sole place where application and concrete adapters meet."""

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
from job_radar.adapters.outbound.jsonl import JsonlJobReader
from job_radar.adapters.outbound.sqlite import MINIMUM_SQLITE, SQLiteJobRepository, read_status
from job_radar.adapters.outbound.telemetry import JsonTelemetry, metrics_snapshot
from job_radar.application.ingest import IngestJobs
from job_radar.application.planning import SearchSource, plan_queries


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
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
                emit(
                    {
                        "executed": False,
                        "config_hash": config_hash,
                        "count": len(plan),
                        "queries": [asdict(query) for query in plan],
                    }
                )
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
    except ValueError, TypeError, yaml.YAMLError, RecursionError:
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
