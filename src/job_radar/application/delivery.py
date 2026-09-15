"""Sequential delivery, durable reservations and provider read-back after each send."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from job_radar.application.ports import Clock, Telemetry
from job_radar.domain.mail import (
    MAX_DAILY_SEND_LIMIT,
    DeliveryBlocked,
    Mail,
    Receipt,
    SentRecord,
)


class MailGateway(Protocol):
    def profile(self) -> str: ...

    def recent_sent(self, since: datetime) -> Sequence[SentRecord]: ...

    def contact_block(self, mail: Mail) -> str | None: ...

    def send(self, mail: Mail) -> str: ...

    def verify(self, mail: Mail, message_id: str) -> Receipt: ...


class DraftGateway(Protocol):
    def profile(self) -> str: ...

    def draft_exists(self, mail: Mail) -> bool: ...

    def draft(self, mail: Mail) -> str: ...


class Outbox(Protocol):
    def pending(self, batch: str) -> Sequence[Mail]: ...

    def reserve(
        self, batch: str, mail: Mail, history: Sequence[SentRecord], now: datetime, limit: int
    ) -> None: ...

    def finish(self, batch: str, mail: Mail, receipt: Receipt, now: datetime) -> None: ...

    def unknown(self, batch: str, mail: Mail, message_id: str | None, now: datetime) -> None: ...

    def block(self, batch: str, mail: Mail, reason: str, now: datetime) -> None: ...


class DraftOutbox(Protocol):
    def pending_drafts(self, batch: str) -> Sequence[Mail]: ...

    def reserve_draft(self, batch: str, mail: Mail, now: datetime) -> None: ...

    def finish_draft(self, batch: str, mail: Mail, draft_id: str, now: datetime) -> None: ...

    def unknown_draft(
        self, batch: str, mail: Mail, draft_id: str | None, now: datetime
    ) -> None: ...

    def block(self, batch: str, mail: Mail, reason: str, now: datetime) -> None: ...


class DeliverBatch:
    def __init__(
        self, outbox: Outbox, gateway: MailGateway, clock: Clock, telemetry: Telemetry
    ) -> None:
        self.outbox, self.gateway, self.clock, self.telemetry = outbox, gateway, clock, telemetry

    def execute(
        self, batch: str, *, confirmed: str, daily_limit: int = MAX_DAILY_SEND_LIMIT
    ) -> dict[str, int]:
        if confirmed != batch or not 1 <= daily_limit <= MAX_DAILY_SEND_LIMIT:
            raise ValueError(
                f"Exact batch confirmation and limit 1..{MAX_DAILY_SEND_LIMIT} required"
            )
        pending = self.outbox.pending(batch)
        result = {"sent": 0, "blocked": 0, "unknown": 0}
        if not pending:
            return result
        if self.gateway.profile() != pending[0].account:
            raise DeliveryBlocked("account_mismatch")
        for mail in pending:
            now = self.clock.now()
            if (
                not timedelta(0)
                <= now - datetime.fromisoformat(mail.verified_at)
                <= timedelta(hours=24)
            ):
                self.outbox.block(batch, mail, "evidence_expired", now)
                result["blocked"] += 1
                continue
            # Refresh before EACH reservation: external/manual sends also consume budget.
            history = self.gateway.recent_sent(now - timedelta(hours=48))
            reason = self.gateway.contact_block(mail)
            if reason:
                self.outbox.block(batch, mail, reason, now)
                result["blocked"] += 1
                continue
            try:
                self.outbox.reserve(batch, mail, history, now, daily_limit)
            except DeliveryBlocked as error:
                self.outbox.block(batch, mail, str(error), now)
                result["blocked"] += 1
                continue
            message_id = None
            try:
                message_id = self.gateway.send(mail)
                receipt = self.gateway.verify(mail, message_id)
                self.outbox.finish(batch, mail, receipt, self.clock.now())
            except Exception:
                # Fail closed, including timeout, read-back failure and interrupted persistence.
                self.outbox.unknown(batch, mail, message_id, self.clock.now())
                result["unknown"] += 1
                break
            result["sent"] += 1
            self.telemetry.event("mail.sent", {"sent": result["sent"]})
        self.telemetry.event("mail.batch_finished", {key: value for key, value in result.items()})
        return result


class DraftBatch:
    """Create reviewable Gmail drafts, never invoke Gmail's send control.

    Draft reservations are not delivery reservations.  A timeout therefore becomes
    an explicit review item instead of a retry that could create a duplicate draft.
    """

    def __init__(
        self, outbox: DraftOutbox, gateway: DraftGateway, clock: Clock, telemetry: Telemetry
    ) -> None:
        self.outbox, self.gateway, self.clock, self.telemetry = outbox, gateway, clock, telemetry

    def execute(self, batch: str, *, confirmed: str) -> dict[str, int]:
        if confirmed != batch:
            raise ValueError("Exact batch confirmation required")
        pending = self.outbox.pending_drafts(batch)
        result = {"drafted": 0, "blocked": 0, "unknown": 0}
        if not pending:
            return result
        if self.gateway.profile() != pending[0].account:
            raise DeliveryBlocked("account_mismatch")
        for mail in pending:
            now = self.clock.now()
            if (
                not timedelta(0)
                <= now - datetime.fromisoformat(mail.verified_at)
                <= timedelta(hours=24)
            ):
                self.outbox.block(batch, mail, "evidence_expired", now)
                result["blocked"] += 1
                continue
            if self.gateway.draft_exists(mail):
                self.outbox.block(batch, mail, "existing_draft_needs_review", now)
                result["blocked"] += 1
                continue
            try:
                self.outbox.reserve_draft(batch, mail, now)
            except DeliveryBlocked as error:
                self.outbox.block(batch, mail, str(error), now)
                result["blocked"] += 1
                continue
            draft_id = None
            try:
                draft_id = self.gateway.draft(mail)
                self.outbox.finish_draft(batch, mail, draft_id, self.clock.now())
            except Exception:
                self.outbox.unknown_draft(batch, mail, draft_id, self.clock.now())
                result["unknown"] += 1
                break
            result["drafted"] += 1
            self.telemetry.event("mail.drafted", {"drafted": result["drafted"]})
        self.telemetry.event("mail.draft_batch_finished", dict(result))
        return result
