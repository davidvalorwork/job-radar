"""Argument parsing only; concrete adapter wiring belongs to bootstrap."""

import argparse
from pathlib import Path

from job_radar.domain.mail import MAX_DAILY_SEND_LIMIT


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="radar", description="Auditable discovery and opt-in mail")
    root.add_argument("--config", type=Path, default=Path("config/example.yaml"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect runtime resources without accessing accounts")
    collect = commands.add_parser("collect", help="Batch Agent Reach reads; compact output, no LLM")
    collect.add_argument("--input", type=Path, required=True)
    collect.add_argument("--data-dir", type=Path, default=Path(".local/research"))
    collect.add_argument("--workers", type=int, default=4)
    collect.add_argument("--ttl-seconds", type=int, default=86400)
    collect.add_argument("--allow-network", action="store_true", required=True)
    expand = commands.add_parser("expand", help="Follow cached public job links; no LLM")
    expand.add_argument("--data-dir", type=Path, default=Path(".local/research"))
    expand.add_argument("--limit", type=int, default=100)
    expand.add_argument("--after", type=int, default=0)
    expand.add_argument(
        "--unseen-only", action="store_true", help="Skip cached OK URLs; starts at zero"
    )
    expand.add_argument("--workers", type=int, default=4)
    expand.add_argument("--ttl-seconds", type=int, default=86400)
    expand.add_argument("--allow-network", action="store_true", required=True)
    normalize = commands.add_parser("normalize", help="Create deterministic local job records")
    normalize.add_argument("--data-dir", type=Path, default=Path(".local/research"))
    normalize.add_argument("--output", type=Path, required=True)
    normalize.add_argument("--limit", type=int, default=100)
    normalize.add_argument("--after", type=int, default=0)
    review = commands.add_parser("review", help="Offline evidence cards with a hard context budget")
    review.add_argument("--data-dir", type=Path, default=Path(".local/research"))
    review.add_argument("--limit", type=int, default=25)
    review.add_argument("--max-chars", type=int, default=12000)
    review.add_argument("--after", type=int, default=0)
    review.add_argument("--snapshot")
    review.add_argument("--seen", type=Path, action="append", default=[])
    review.add_argument(
        "--outbox", type=Path, help="Same-account outbox: suppressions and 48h filter"
    )
    review.add_argument("--output", type=Path)
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
            command.add_argument(
                "--output", type=Path, help="Save plan privately; print counts only"
            )
    for name in ("init", "ingest", "status", "metrics"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=Path(".local/radar"))
        if name == "ingest":
            command.add_argument("--input", type=Path, required=True)
    mail = commands.add_parser("mail", help="Explicitly authorized Gmail connector delivery")
    operations = mail.add_subparsers(dest="mail_command", required=True)
    hold = operations.add_parser(
        "hold", help="Hold an obsolete prepared batch; never deletes or sends"
    )
    hold.add_argument("--batch", required=True)
    hold.add_argument("--confirm", required=True)
    hold.add_argument("--data-dir", type=Path, default=Path(".local/mail"))
    external = operations.add_parser(
        "reconcile-external", help="Review/import external sent receipts"
    )
    external.add_argument("--input", type=Path, required=True)
    external.add_argument("--data-dir", type=Path, default=Path(".local/mail"))
    external.add_argument("--confirm", help="Exact digest from read-only preview")
    audit = operations.add_parser(
        "audit", help="Offline ledger coverage; never sends or reconciles"
    )
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--data-dir", type=Path, default=Path(".local/mail"))
    check = operations.add_parser("check", help="Offline OAuth readiness; never sends or refreshes")
    check.add_argument("--credentials", type=Path, required=True)
    auth = operations.add_parser("auth", help="One-time direct Gmail OAuth setup; never sends")
    auth.add_argument("--client-secret", type=Path, required=True)
    auth.add_argument("--credentials", type=Path, required=True)
    auth.add_argument("--account", required=True)
    compose = operations.add_parser("compose", help="Render reviewed facts into messages; no LLM")
    compose.add_argument("--input", type=Path, required=True)
    compose.add_argument("--profile", type=Path, required=True)
    compose.add_argument("--output", type=Path, required=True)
    for name in ("prepare", "status", "run", "draft", "reconcile"):
        command = operations.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=Path(".local/mail"))
        if name == "prepare":
            command.add_argument("--manifest", type=Path, required=True)
        if name in {"run", "draft", "reconcile"}:
            command.add_argument("--batch", required=True)
            command.add_argument(
                "--bridge-timeout",
                type=float,
                default=90,
                help="Local bridge deadline in seconds, maximum 300",
            )
            if name == "draft":
                command.add_argument("--bridge-dir", type=Path, required=True)
            else:
                transport = command.add_mutually_exclusive_group(required=True)
                transport.add_argument("--bridge-dir", type=Path)
                transport.add_argument(
                    "--credentials", type=Path, help="Direct Gmail OAuth; no assistant"
                )
        if name in {"run", "draft"}:
            command.add_argument("--confirm", required=True, help="Exact approved batch hash")
        if name == "run":
            command.add_argument("--daily-limit", type=int, default=MAX_DAILY_SEND_LIMIT)
        if name == "reconcile":
            command.add_argument("--mail-id", required=True)
            command.add_argument("--message-id")
    return root
