"""Read-only, account-scoped ledger coverage for an explicitly supplied provider ID set.

This does not contact Gmail, trust IDs as proof of delivery, or authorize replay.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated

from pydantic import Field

from job_radar.adapters.outbound.config import StrictModel
from job_radar.domain.mail import address


class AuditInput(StrictModel):
    account: str = Field(max_length=254)
    message_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=1, max_length=500
    )


def audit_ledger(source: Path, outbox: Path) -> dict[str, int | bool | None]:
    with source.open("rb") as stream:
        data = stream.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("Audit input exceeds 1 MB")
    request = AuditInput.model_validate_json(data)
    account = address(request.account)
    ids = set(request.message_ids)
    initialized = outbox.exists()
    states: dict[str, str] = {}
    external: set[str] = set()
    if initialized:
        uri = outbox.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            marks = ",".join("?" for _ in ids)
            states = dict(
                connection.execute(
                    "SELECT provider_id,state FROM outbox WHERE account=? "
                    f"AND provider_id IN ({marks})",
                    (account, *sorted(ids)),
                )
            )
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_sends'"
            ).fetchone():
                external = {
                    row[0]
                    for row in connection.execute(
                        "SELECT provider_id FROM external_sends WHERE account=? "
                        f"AND provider_id IN ({marks})",
                        (account, *sorted(ids)),
                    )
                }
    sent = sum(state == "sent" for state in states.values())
    return {
        "initialized": initialized,
        "unique_input_ids": len(ids),
        "duplicate_input_ids": len(request.message_ids) - len(ids),
        "ledger_sent": sent,
        "ledger_not_sent": len(states) - sent,
        "external_sent": len(external),
        "untracked": len(ids - states.keys() - external),
        "ledger_complete": sent + len(external - states.keys()) == len(ids),
        "provider_verified": False,
        "valid_applications": None,
        "network_enabled": False,
        "sending_enabled": False,
        "llm_calls": 0,
    }
