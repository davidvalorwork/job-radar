"""Read-only Chrome bridge smoke test. Never composes, sends or changes outbox state."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_radar.adapters.outbound.gmail_bridge import FileConnector, GmailBridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, default=Path(".local/chrome/bridge"))
    parser.add_argument("--account", required=True)
    parser.add_argument(
        "--history", action="store_true", help="Also verify complete 48h SENT history"
    )
    args = parser.parse_args(argv)
    gateway = GmailBridge(FileConnector(args.bridge_dir, timeout=300 if args.history else 30))
    stage = "profile"
    try:
        matched = gateway.profile() == args.account.lower()
        print(json.dumps({"account_matches": matched, "sent": 0, "llm_calls": 0}), flush=True)
        if not matched:
            return 1
        if args.history:
            stage = "history"
            records = gateway.recent_sent(datetime.now(UTC) - timedelta(hours=48))
            print(
                json.dumps(
                    {"history_complete": True, "messages": len(records), "sent": 0, "llm_calls": 0}
                ),
                flush=True,
            )
        return 0
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r"connector_[a-z_]{1,90}|incomplete_history|invalid_history", code):
            code = "probe_failed"
        print(
            json.dumps({"stage": stage, "error_code": code, "sent": 0, "llm_calls": 0}), flush=True
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
