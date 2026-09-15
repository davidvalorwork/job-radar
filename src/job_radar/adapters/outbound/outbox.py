"""SQLite outbox in a separate database: same-account quota/cooldown reservations are atomic."""

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_radar.adapters.outbound.sqlite import MINIMUM_SQLITE
from job_radar.domain.mail import DeliveryBlocked, Mail, Receipt, SentRecord, address

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 batch TEXT NOT NULL REFERENCES batches(id), id TEXT NOT NULL, account TEXT NOT NULL,
 recipient TEXT NOT NULL, company_key TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','sending','sent','unknown')),
 provider_id TEXT, sent_at TEXT, PRIMARY KEY(batch,id));
CREATE INDEX IF NOT EXISTS outbox_guards ON outbox(account,state,recipient,company_key);
CREATE UNIQUE INDEX IF NOT EXISTS provider_unique ON outbox(account,provider_id)
 WHERE provider_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS drafts(
 batch TEXT NOT NULL, id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('drafting','drafted','unknown')),
 provider_id TEXT, drafted_at TEXT,
 PRIMARY KEY(batch,id),
 FOREIGN KEY(batch,id) REFERENCES outbox(batch,id));
CREATE UNIQUE INDEX IF NOT EXISTS draft_provider_unique ON drafts(provider_id)
 WHERE provider_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS history(
 account TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL, recipients TEXT NOT NULL,
 PRIMARY KEY(account,id));
CREATE TABLE IF NOT EXISTS external_sends(
 account TEXT NOT NULL, provider_id TEXT NOT NULL, recipient TEXT NOT NULL,
 company_key TEXT NOT NULL, sent_at TEXT NOT NULL, subject_sha256 TEXT NOT NULL,
 evidence_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(account,provider_id));
CREATE TABLE IF NOT EXISTS mail_holds(
 batch TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(batch,id), FOREIGN KEY(batch,id) REFERENCES outbox(batch,id));
CREATE TABLE IF NOT EXISTS suppressions(
 account TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(account,kind,value));
CREATE TABLE IF NOT EXISTS mail_audit(
 sequence INTEGER PRIMARY KEY, at TEXT NOT NULL, batch TEXT NOT NULL,
 mail_id TEXT, event TEXT NOT NULL, reason TEXT);
PRAGMA user_version=1;
"""


class SQLiteOutbox:
    def __init__(self, path: Path) -> None:
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
            raise RuntimeError("Unsupported SQLite")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise RuntimeError("Unsupported outbox schema")
            self.connection.execute("PRAGMA foreign_keys=ON")
            mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("Outbox requires WAL on a local filesystem")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(SCHEMA)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def reconcile_external(
        self, account: str, records: Sequence[dict[str, str]], evidence_sha256: str, now: datetime
    ) -> int:
        """Import reviewed provider read-back, never invent preparation/delivery events.

        The adapter caller verifies exact IDs and headers before this transaction.
        Stable receipt conflicts abort the entire import; retries are idempotent.
        """
        account = address(account)
        if (
            not 1 <= len(records) <= 500
            or now.utcoffset() is None
            or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256)
        ):
            raise ValueError("Invalid external reconciliation")
        inserted = 0
        with self.transaction():
            for item in records:
                recipient = address(item["recipient"])
                company = item["company_key"]
                at = datetime.fromisoformat(item["at"])
                if (
                    not item["id"].strip()
                    or len(item["id"]) > 256
                    or at.utcoffset() is None
                    or at > now + timedelta(minutes=1)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["subject_sha256"])
                    or not (
                        company == f"contact:{recipient}"
                        or re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", company)
                    )
                ):
                    raise ValueError("Invalid external receipt")
                stamp = at.astimezone(UTC).isoformat()
                stable = (recipient, company, stamp, item["subject_sha256"])
                existing = self.connection.execute(
                    "SELECT recipient,company_key,sent_at,subject_sha256 FROM external_sends "
                    "WHERE account=? AND provider_id=?",
                    (account, item["id"]),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != stable:
                        raise ValueError("External receipt conflict")
                    continue
                if self.connection.execute(
                    "SELECT 1 FROM outbox WHERE account=? AND provider_id=?",
                    (account, item["id"]),
                ).fetchone():
                    raise ValueError("Managed receipt conflict; use ordinary reconciliation")
                previous = self.connection.execute(
                    "SELECT at,recipients FROM history WHERE account=? AND id=?",
                    (account, item["id"]),
                ).fetchone()
                if previous and (
                    datetime.fromisoformat(previous[0]) != at
                    or json.loads(previous[1]) != [recipient]
                ):
                    raise ValueError("History receipt conflict")
                self.connection.execute(
                    "INSERT INTO external_sends VALUES (?,?,?,?,?,?,?,?)",
                    (account, item["id"], *stable, evidence_sha256, now.isoformat()),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO history VALUES (?,?,?,?)",
                    (account, item["id"], stamp, json.dumps([recipient])),
                )
                self.audit(evidence_sha256, item["id"], "external_sent_reconciled", now)
                inserted += 1
        return inserted

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def audit(
        self, batch: str, mail_id: str | None, event: str, now: datetime, reason: str | None = None
    ) -> None:
        self.connection.execute(
            "INSERT INTO mail_audit(at,batch,mail_id,event,reason) VALUES (?,?,?,?,?)",
            (now.isoformat(), batch, mail_id, event, reason),
        )

    def prepare(
        self,
        batch: str,
        mails: Sequence[Mail],
        now: datetime,
        recipients: Sequence[str] = (),
        companies: Sequence[str] = (),
    ) -> None:
        if not mails or len({mail.account for mail in mails}) != 1:
            raise ValueError("Nonempty single-account batch required")
        with self.transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO batches VALUES (?,?)", (batch, now.isoformat())
            )
            for mail in mails:
                changed = self.connection.execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,'prepared',NULL,NULL)",
                    (
                        batch,
                        mail.key,
                        mail.account,
                        mail.recipient,
                        mail.company_key,
                        json.dumps(asdict(mail), sort_keys=True),
                    ),
                ).rowcount
                if changed:
                    self.audit(batch, mail.key, "prepared", now)
            for kind, values in (("recipient", recipients), ("company", companies)):
                for value in values:
                    clean = address(value) if kind == "recipient" else value.strip().lower()
                    self.connection.execute(
                        "INSERT OR IGNORE INTO suppressions VALUES (?,?,?)",
                        (mails[0].account, kind, clean),
                    )

    def pending(self, batch: str) -> list[Mail]:
        if not self.connection.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise ValueError("Unknown batch")
        rows = self.connection.execute(
            "SELECT o.payload FROM outbox o LEFT JOIN drafts d ON d.batch=o.batch AND d.id=o.id "
            "WHERE o.batch=? AND o.state='prepared' AND d.id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM mail_holds h WHERE h.batch=o.batch AND h.id=o.id) "
            "ORDER BY o.rowid",
            (batch,),
        )
        return [Mail(**json.loads(row[0])) for row in rows]

    def pending_drafts(self, batch: str) -> list[Mail]:
        """Return prepared messages that have never been claimed as a Gmail draft.

        A draft claim is deliberately separate from the delivery state: creating a
        draft must neither count against a sending quota nor allow a later `run`
        command to send it without the reviewer choosing to do so.
        """
        if not self.connection.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise ValueError("Unknown batch")
        rows = self.connection.execute(
            "SELECT o.payload FROM outbox o LEFT JOIN drafts d ON d.batch=o.batch AND d.id=o.id "
            "WHERE o.batch=? AND o.state='prepared' AND d.id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM mail_holds h WHERE h.batch=o.batch AND h.id=o.id) "
            "ORDER BY o.rowid",
            (batch,),
        )
        return [Mail(**json.loads(row[0])) for row in rows]

    def reserve_draft(self, batch: str, mail: Mail, now: datetime) -> None:
        """Durably claim a message before Gmail is asked to create a draft."""
        with self.transaction():
            self._check_hold(batch, mail.key)
            if self.connection.execute(
                "SELECT 1 FROM suppressions WHERE account=? AND "
                "((kind='recipient' AND value=?) OR (kind='company' AND value=?))",
                (mail.account, mail.recipient, mail.company_key),
            ).fetchone():
                raise DeliveryBlocked("suppressed")
            changed = self.connection.execute(
                "INSERT INTO drafts(batch,id,state,provider_id,drafted_at) "
                "SELECT ?,?,'drafting',NULL,NULL FROM outbox "
                "WHERE batch=? AND id=? AND state='prepared'",
                (batch, mail.key, batch, mail.key),
            ).rowcount
            if changed != 1:
                raise DeliveryBlocked("draft_already_claimed")
            self.audit(batch, mail.key, "drafting", now)

    def finish_draft(self, batch: str, mail: Mail, draft_id: str, now: datetime) -> None:
        if not draft_id:
            raise ValueError("Invalid draft identifier")
        with self.transaction():
            changed = self.connection.execute(
                "UPDATE drafts SET state='drafted',provider_id=?,drafted_at=? "
                "WHERE batch=? AND id=? AND state='drafting'",
                (draft_id, now.isoformat(), batch, mail.key),
            ).rowcount
            if changed != 1:
                raise ValueError("No active draft reservation")
            self.audit(batch, mail.key, "drafted_verified", now)

    def unknown_draft(self, batch: str, mail: Mail, draft_id: str | None, now: datetime) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE drafts SET state='unknown',provider_id=? WHERE batch=? AND id=? "
                "AND state='drafting'",
                (draft_id, batch, mail.key),
            )
            self.audit(batch, mail.key, "draft_unknown", now, "review_required")

    def get(self, batch: str, key: str) -> tuple[Mail, str | None]:
        row = self.connection.execute(
            "SELECT payload,provider_id FROM outbox WHERE batch=? AND id=? "
            "AND state IN ('sending','unknown')",
            (batch, key),
        ).fetchone()
        if row is None:
            raise ValueError("No uncertain send found")
        return Mail(**json.loads(row[0])), row[1]

    def reserve(
        self, batch: str, mail: Mail, history: Sequence[SentRecord], now: datetime, limit: int
    ) -> None:
        with self.transaction():
            self._check_hold(batch, mail.key)
            for record in history:
                if record.at.utcoffset() is None or record.at > now + timedelta(minutes=1):
                    raise DeliveryBlocked("invalid_history_clock")
                self.connection.execute(
                    "INSERT OR REPLACE INTO history VALUES (?,?,?,?)",
                    (mail.account, record.id, record.at.isoformat(), json.dumps(record.recipients)),
                )
            if self.connection.execute(
                "SELECT 1 FROM suppressions WHERE account=? AND "
                "((kind='recipient' AND value=?) OR (kind='company' AND value=?))",
                (mail.account, mail.recipient, mail.company_key),
            ).fetchone():
                raise DeliveryBlocked("suppressed")
            rows = self.connection.execute(
                "SELECT state,recipient,company_key,provider_id,sent_at FROM outbox "
                "WHERE account=? AND state!='prepared' UNION ALL "
                "SELECT 'sent',recipient,company_key,provider_id,sent_at FROM external_sends "
                "WHERE account=?",
                (mail.account, mail.account),
            ).fetchall()
            if any(row["state"] in ("sending", "unknown") for row in rows):
                raise DeliveryBlocked("unresolved_send")
            combined = {
                row["id"]: (datetime.fromisoformat(row["at"]), tuple(json.loads(row["recipients"])))
                for row in self.connection.execute(
                    "SELECT * FROM history WHERE account=?", (mail.account,)
                )
            }
            for row in rows:
                at = datetime.fromisoformat(row["sent_at"])
                combined.setdefault(row["provider_id"], (at, (row["recipient"],)))
                if now - at < timedelta(hours=48) and row["company_key"] == mail.company_key:
                    raise DeliveryBlocked("company_cooldown")
            spent = 0
            for at, recipients in combined.values():
                if now - at < timedelta(hours=24):
                    spent += max(1, len(recipients))
                if now - at < timedelta(hours=48) and any(
                    address(value) == mail.recipient
                    or value.rsplit("@", 1)[-1].lower() == mail.company_key
                    for value in recipients
                ):
                    raise DeliveryBlocked("recipient_or_domain_cooldown")
            if spent >= limit:
                raise DeliveryBlocked("daily_quota")
            changed = self.connection.execute(
                "UPDATE outbox SET state='sending' WHERE batch=? AND id=? AND state='prepared'",
                (batch, mail.key),
            ).rowcount
            if changed != 1:
                raise DeliveryBlocked("already_claimed")
            self.audit(batch, mail.key, "sending", now)

    def finish(self, batch: str, mail: Mail, receipt: Receipt, now: datetime) -> None:
        if receipt.at.utcoffset() is None or not receipt.id:
            raise ValueError("Invalid receipt")
        with self.transaction():
            reservation = self.connection.execute(
                "SELECT at FROM mail_audit WHERE batch=? AND mail_id=? AND event='sending' "
                "ORDER BY sequence DESC LIMIT 1",
                (batch, mail.key),
            ).fetchone()
            if reservation is None or receipt.at < datetime.fromisoformat(
                reservation[0]
            ) - timedelta(minutes=1):
                raise ValueError("Receipt predates reservation")
            changed = self.connection.execute(
                "UPDATE outbox SET state='sent',provider_id=?,sent_at=? WHERE batch=? AND id=? "
                "AND state IN ('sending','unknown')",
                (receipt.id, receipt.at.isoformat(), batch, mail.key),
            ).rowcount
            if changed != 1:
                raise ValueError("No active reservation")
            self.audit(batch, mail.key, "sent_verified", now)

    def unknown(self, batch: str, mail: Mail, message_id: str | None, now: datetime) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE outbox SET state='unknown',provider_id=? WHERE batch=? AND id=? "
                "AND state='sending'",
                (message_id, batch, mail.key),
            )
            self.audit(batch, mail.key, "unknown", now, "reconciliation_required")

    def block(self, batch: str, mail: Mail, reason: str, now: datetime) -> None:
        with self.transaction():
            if reason in {"suppressed", "provider_suppression"}:
                self.connection.execute(
                    "INSERT OR IGNORE INTO suppressions VALUES (?,?,?)",
                    (mail.account, "recipient", mail.recipient),
                )
            self.audit(batch, mail.key, "blocked", now, reason)

    def status(self) -> dict[str, object]:
        return {
            "held_prepared": self.connection.execute("SELECT count(*) FROM mail_holds").fetchone()[
                0
            ],
            "external_sent": self.connection.execute(
                "SELECT count(*) FROM external_sends"
            ).fetchone()[0],
            "unresolved": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT batch,id,state,provider_id FROM outbox "
                    "WHERE state IN ('sending','unknown')"
                )
            ],
            "states": dict(
                self.connection.execute("SELECT state,count(*) FROM outbox GROUP BY state")
            ),
            "drafts": dict(
                self.connection.execute("SELECT state,count(*) FROM drafts GROUP BY state")
            ),
            "unresolved_drafts": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT batch,id,state,provider_id FROM drafts "
                    "WHERE state IN ('drafting','unknown')"
                )
            ],
            "audit_events": self.connection.execute("SELECT count(*) FROM mail_audit").fetchone()[
                0
            ],
            "suppression_entries": self.connection.execute(
                "SELECT count(*) FROM suppressions"
            ).fetchone()[0],
        }

    def _check_hold(self, batch: str, key: str) -> None:
        if self.connection.execute(
            "SELECT 1 FROM mail_holds WHERE batch=? AND id=?", (batch, key)
        ).fetchone():
            raise DeliveryBlocked("batch_held_for_review")

    def hold_batch(self, batch: str, now: datetime) -> int:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id,state FROM outbox WHERE batch=?", (batch,)
            ).fetchall()
            if not rows or any(row["state"] in {"sending", "unknown"} for row in rows):
                raise ValueError("Unknown or actively sending batch cannot be held")
            count = 0
            for row in rows:
                if row["state"] != "prepared":
                    continue
                changed = self.connection.execute(
                    "INSERT OR IGNORE INTO mail_holds VALUES (?,?,?)",
                    (batch, row["id"], now.isoformat()),
                ).rowcount
                if changed:
                    self.audit(batch, row["id"], "held_for_review", now)
                    count += 1
            return count
