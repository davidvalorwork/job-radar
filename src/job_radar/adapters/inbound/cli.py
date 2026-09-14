"""Argument parsing only; concrete adapter wiring belongs to bootstrap."""

import argparse
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="radar", description="Offline, auditable job discovery")
    root.add_argument("--config", type=Path, default=Path("config/example.yaml"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect runtime resources without accessing accounts")
    config = commands.add_parser("config", help="Validate configuration and source catalog")
    config.add_subparsers(dest="config_command", required=True).add_parser("validate")
    for name in ("catalog", "plan"):
        command = commands.add_parser(name)
        command.add_argument(
            "--category",
            action="append",
            default=[],
            choices=[
                "web_search",
                "social",
                "job_board",
                "community",
                "company_page",
                "feed",
            ],
        )
        command.add_argument("--source", action="append", default=[])
        if name == "catalog":
            command.add_argument("--enabled-only", action="store_true")
        else:
            command.add_argument("--limit", type=int)
    for name in ("init", "ingest", "status", "metrics"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=Path(".local/radar"))
        if name == "ingest":
            command.add_argument("--input", type=Path, required=True)
    return root
