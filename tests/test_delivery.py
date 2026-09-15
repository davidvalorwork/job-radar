import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.application.delivery import DeliverBatch, DraftBatch
from job_radar.domain.mail import DeliveryBlocked, Receipt, SentRecord, address


@pytest.fixture
def box(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "mail.db")
    yield outbox
    outbox.close()


class Clock:
    def now(self):
        return datetime.now(UTC)


class Telemetry:
    def __init__(self):
        self.events = []

    def event(self, name, fields):
        self.events.append((name, fields))


class Gateway:
    def __init__(self, mail):
        self.mail = mail
        self.history = []
        self.sent = []
        self.reason = None
        self.failure = None
        self.account = mail.account
        self.drafts = []
        self.existing_draft = False

    def profile(self):
        return self.account

    def recent_sent(self, since):
        return self.history

    def contact_block(self, mail):
        return self.reason

    def send(self, mail):
        self.sent.append(mail)
        if self.failure == "send":
            raise TimeoutError("uncertain transport")
        return "provider-1"

    def verify(self, mail, message_id):
        if self.failure == "verify":
            raise RuntimeError("read back unavailable")
        return Receipt(message_id, datetime.now(UTC))

    def draft_exists(self, mail):
        return self.existing_draft

    def draft(self, mail):
        self.drafts.append(mail)
        if self.failure == "draft":
            raise TimeoutError("uncertain draft transport")
        return "draft-1"


def setup(box, mail):
    box.prepare("batch", [mail], datetime.now(UTC))
    gateway, telemetry = Gateway(mail), Telemetry()
    return DeliverBatch(box, gateway, Clock(), telemetry), gateway, telemetry


def test_delivery_and_repeat(box, mail):
    delivery, gateway, telemetry = setup(box, mail)
    assert delivery.execute("batch", confirmed="batch") == {"sent": 1, "blocked": 0, "unknown": 0}
    assert delivery.execute("batch", confirmed="batch")["sent"] == 0
    box.prepare("batch", [mail], datetime.now(UTC))
    assert len(gateway.sent) == 1
    assert box.status()["states"] == {"sent": 1}
    assert box.status()["audit_events"] == 3
    assert mail.recipient not in json.dumps(telemetry.events)


def test_draft_batch_never_sends_and_is_not_repeated(box, mail):
    box.prepare("batch", [mail], datetime.now(UTC))
    gateway, telemetry = Gateway(mail), Telemetry()
    drafts = DraftBatch(box, gateway, Clock(), telemetry)
    assert drafts.execute("batch", confirmed="batch") == {"drafted": 1, "blocked": 0, "unknown": 0}
    assert gateway.sent == []
    assert gateway.drafts == [mail]
    assert box.pending("batch") == []
    assert box.status()["drafts"] == {"drafted": 1}
    assert drafts.execute("batch", confirmed="batch")["drafted"] == 0
    assert mail.recipient not in json.dumps(telemetry.events)


def test_draft_timeout_is_reviewed_and_never_retried(box, mail):
    box.prepare("batch", [mail], datetime.now(UTC))
    gateway = Gateway(mail)
    gateway.failure = "draft"
    drafts = DraftBatch(box, gateway, Clock(), Telemetry())
    assert drafts.execute("batch", confirmed="batch") == {"drafted": 0, "blocked": 0, "unknown": 1}
    assert drafts.execute("batch", confirmed="batch") == {"drafted": 0, "blocked": 0, "unknown": 0}
    assert box.status()["unresolved_drafts"][0]["state"] == "unknown"


def test_existing_gmail_draft_is_not_replaced(box, mail):
    box.prepare("batch", [mail], datetime.now(UTC))
    gateway = Gateway(mail)
    gateway.existing_draft = True
    result = DraftBatch(box, gateway, Clock(), Telemetry()).execute("batch", confirmed="batch")
    assert result == {"drafted": 0, "blocked": 1, "unknown": 0}
    assert gateway.drafts == []


def test_recruiter_identity_matches_recipient(mail):
    assert replace(mail, company_key="contact:jobs@company.example").company_key.startswith(
        "contact:"
    )
    with pytest.raises(ValueError, match="Contact identity"):
        replace(mail, company_key="contact:other@company.example")


def test_independent_recruiters_do_not_share_domain_cooldown(box, mail):
    now = datetime.now(UTC)
    first = replace(mail, recipient="a@shared.example", company_key="contact:a@shared.example")
    second = replace(mail, recipient="b@shared.example", company_key="contact:b@shared.example")
    box.prepare("first", [first], now)
    box.reserve("first", first, [], now, 100)
    box.finish("first", first, Receipt("first-id", now), now)
    box.prepare("second", [second], now)
    box.reserve("second", second, [], now, 100)


def test_recruiter_exact_recipient_cooldown_still_applies(box, mail):
    now = datetime.now(UTC)
    candidate = replace(mail, company_key="contact:jobs@company.example")
    box.prepare("batch", [candidate], now)
    with pytest.raises(DeliveryBlocked, match="recipient_or_domain_cooldown"):
        box.reserve("batch", candidate, [SentRecord("manual", (mail.recipient,), now)], now, 100)


@pytest.mark.parametrize("failure", ["send", "verify"])
def test_unknown_blocks_all_new_sends_and_reconciles(box, mail, failure):
    delivery, gateway, _ = setup(box, mail)
    gateway.failure = failure
    assert delivery.execute("batch", confirmed="batch")["unknown"] == 1
    assert box.status()["unresolved"][0]["id"] == mail.key
    candidate, provider_id = box.get("batch", mail.key)
    assert provider_id == (None if failure == "send" else "provider-1")
    assert candidate == mail
    second = replace(mail, recipient="jobs@other.example", company_key="other.example")
    box.prepare("second", [second], datetime.now(UTC))
    assert delivery.execute("second", confirmed="second")["blocked"] == 1
    assert len(gateway.sent) == 1
    box.finish("batch", mail, Receipt("provider-1", datetime.now(UTC)), datetime.now(UTC))
    assert box.status()["states"] == {"sent": 1, "prepared": 1}


@pytest.mark.parametrize("hours", [-1, 25])
def test_evidence_expiry(box, mail, hours):
    mail = replace(mail, verified_at=(datetime.now(UTC) - timedelta(hours=hours)).isoformat())
    delivery, gateway, _ = setup(box, mail)
    assert delivery.execute("batch", confirmed="batch")["blocked"] == 1
    assert gateway.sent == []


def test_account_confirmation_and_limit(box, mail):
    delivery, gateway, _ = setup(box, mail)
    for confirmation, limit in [("wrong", 100), ("batch", 0), ("batch", 501)]:
        with pytest.raises(ValueError):
            delivery.execute("batch", confirmed=confirmation, daily_limit=limit)
    gateway.account = "different@example.com"
    with pytest.raises(DeliveryBlocked, match="account_mismatch"):
        delivery.execute("batch", confirmed="batch")
    assert gateway.sent == []
    accepted, _, _ = setup(box, mail)
    assert accepted.execute("batch", confirmed="batch", daily_limit=500)["sent"] == 1


def test_provider_suppression_persists(box, mail):
    delivery, gateway, _ = setup(box, mail)
    gateway.reason = "provider_suppression"
    assert delivery.execute("batch", confirmed="batch")["blocked"] == 1
    assert box.status()["suppression_entries"] == 1
    gateway.reason = None
    assert delivery.execute("batch", confirmed="batch")["blocked"] == 1
    assert gateway.sent == []


@pytest.mark.parametrize("hours,blocked", [(47.99, True), (48, False), (480, False)])
@pytest.mark.parametrize("recipient", ["jobs@company.example", "other@company.example"])
def test_history_cooldown_boundary(box, mail, hours, blocked, recipient):
    now = datetime.now(UTC)
    box.prepare("batch", [mail], now)
    history = [SentRecord("old", (recipient,), now - timedelta(hours=hours))]
    if blocked:
        with pytest.raises(DeliveryBlocked, match="cooldown"):
            box.reserve("batch", mail, history, now, 100)
    else:
        box.reserve("batch", mail, history, now, 100)


def test_recent_manual_mail_uses_quota_and_not_provider_domain_cooldown(box, mail):
    now = datetime.now(UTC)
    mail = replace(mail, recipient="recruiter@gmail.com")
    box.prepare("batch", [mail], now)
    history = [SentRecord("manual", ("unrelated@gmail.com",), now)]
    with pytest.raises(DeliveryBlocked, match="daily_quota"):
        box.reserve("batch", mail, history, now, 1)
    box.reserve("batch", mail, history, now, 2)
    assert box.status()["states"] == {"sending": 1}


def test_provider_and_local_receipt_count_once(box, mail):
    now = datetime.now(UTC)
    box.prepare("batch", [mail], now)
    box.reserve("batch", mail, [], now, 100)
    box.finish("batch", mail, Receipt("p1", now), now)
    second = replace(mail, recipient="jobs@other.example", company_key="other.example")
    box.prepare("second", [second], now)
    box.reserve("second", second, [SentRecord("p1", (mail.recipient,), now)], now, 2)


def test_local_company_cooldown_and_claim_state(box, mail):
    now = datetime.now(UTC)
    box.prepare("batch", [mail], now)
    box.reserve("batch", mail, [], now, 100)
    box.finish("batch", mail, Receipt("p1", now), now)
    second = replace(mail, recipient="other@another.example")
    box.prepare("second", [second], now)
    with pytest.raises(DeliveryBlocked, match="company_cooldown"):
        box.reserve("second", second, [], now, 100)
    with pytest.raises(DeliveryBlocked, match="already_claimed"):
        box.reserve("batch", mail, [], now + timedelta(hours=49), 100)


@pytest.mark.parametrize("kind", ["recipient", "company"])
def test_manifest_suppressions(box, mail, kind):
    kwargs = (
        {"recipients": [mail.recipient]}
        if kind == "recipient"
        else {"companies": [mail.company_key]}
    )
    box.prepare("batch", [mail], datetime.now(UTC), **kwargs)
    with pytest.raises(DeliveryBlocked, match="suppressed"):
        box.reserve("batch", mail, [], datetime.now(UTC), 100)


def test_invalid_outbox_calls(box, mail):
    now = datetime.now(UTC)
    for mails in [[], [mail, replace(mail, account="other@example.com")]]:
        with pytest.raises(ValueError):
            box.prepare("batch", mails, now)
    with pytest.raises(ValueError):
        box.pending("missing")
    with pytest.raises(ValueError):
        box.get("missing", mail.key)
    box.prepare("batch", [mail], now)
    for at in [now.replace(tzinfo=None), now + timedelta(hours=2)]:
        with pytest.raises(DeliveryBlocked, match="invalid_history_clock"):
            box.reserve("batch", mail, [SentRecord("bad", ("x@example.com",), at)], now, 100)
    for receipt in [Receipt("", now), Receipt("x", now.replace(tzinfo=None)), Receipt("x", now)]:
        with pytest.raises(ValueError):
            box.finish("batch", mail, receipt, now)
    box.reserve("batch", mail, [], now, 100)
    with pytest.raises(ValueError, match="predates"):
        box.finish("batch", mail, Receipt("x", now - timedelta(days=1)), now)
    box.finish("batch", mail, Receipt("x", now), now)
    with pytest.raises(ValueError, match="No active"):
        box.finish("batch", mail, Receipt("x", now), now)


def test_atomic_reservation_audit_rollback(box, mail):
    now = datetime.now(UTC)
    box.prepare("batch", [mail], now)
    box.connection.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON mail_audit
      WHEN NEW.event='sending' BEGIN SELECT RAISE(ABORT, 'test'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        box.reserve("batch", mail, [], now, 100)
    assert box.status()["states"] == {"prepared": 1}


def test_concurrent_claims(tmp_path, mail):
    path = tmp_path / "parallel.db"
    box = SQLiteOutbox(path)
    box.prepare("batch", [mail], datetime.now(UTC))
    box.close()

    def claim(_):
        repo = SQLiteOutbox(path)
        try:
            repo.reserve("batch", mail, [], datetime.now(UTC), 100)
            return "reserved"
        except DeliveryBlocked:
            return "blocked"
        finally:
            repo.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == ["blocked", "reserved"]


def test_outbox_schema_guards(tmp_path, monkeypatch):
    path = tmp_path / "new.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    with pytest.raises(RuntimeError):
        SQLiteOutbox(path)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 1, 0))
    with pytest.raises(RuntimeError):
        SQLiteOutbox(tmp_path / "old.db")


def test_mail_validation(mail):
    assert address(" Sender@Example.com ") == "sender@example.com"
    for value in [
        "two@example.com,other@example.com",
        "a@example.com\r\nBcc:x@example.com",
        "x" * 260 + "@example.com",
    ]:
        with pytest.raises(ValueError):
            address(value)
    for field, value in [
        ("company_key", "invalid"),
        ("subject", "\nInjected"),
        ("subject", " "),
        ("body", " "),
        ("body", "bad\0"),
        ("verified_at", "2026-09-14T12:00:00"),
    ]:
        with pytest.raises(ValueError):
            replace(mail, **{field: value})
