import json
from dataclasses import replace
from datetime import timedelta

import pytest

from job_radar.adapters.outbound.mail_audit import audit_ledger
from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.adapters.outbound.review import exclusions
from job_radar.domain.mail import DeliveryBlocked, Receipt, SentRecord


@pytest.fixture
def box(tmp_path):
    ledger = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    try:
        yield ledger
    finally:
        ledger.close()


def record(mail, now):
    return {
        "id": "external-1",
        "recipient": mail.recipient,
        "company_key": mail.company_key,
        "at": now.isoformat(),
        "subject_sha256": "a" * 64,
    }


def test_import_is_atomic_idempotent_and_not_a_fictional_reservation(box, mail, now, tmp_path):
    rows = [record(mail, now)]
    assert box.reconcile_external(mail.account, rows, "b" * 64, now) == 1
    assert box.reconcile_external(mail.account, rows, "c" * 64, now) == 0
    assert box.status()["states"] == {}
    assert box.status()["external_sent"] == 1
    assert box.connection.execute("SELECT count(*) FROM batches").fetchone()[0] == 0
    assert box.connection.execute("SELECT count(*) FROM mail_audit").fetchone()[0] == 1
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"account": mail.account, "message_ids": ["external-1"]}))
    assert audit_ledger(path, tmp_path / "outbox.sqlite3")["ledger_complete"] is True
    recipients, companies = exclusions(tmp_path / "outbox.sqlite3", now)
    assert mail.recipient in recipients and mail.company_key in companies


def test_external_send_prevents_duplicate_and_consumes_quota_once(box, mail, now):
    box.reconcile_external(mail.account, [record(mail, now)], "b" * 64, now)
    box.prepare("batch", [mail], now)
    with pytest.raises(DeliveryBlocked, match="cooldown"):
        box.reserve("batch", mail, [], now, 500)
    other = replace(mail, recipient="other@different.example", company_key="different.example")
    box.prepare("next", [other], now)
    history = [SentRecord("external-1", (mail.recipient,), now)]
    with pytest.raises(DeliveryBlocked, match="daily_quota"):
        box.reserve("next", other, history, now, 1)
    box.reserve("next", other, history, now, 2)


def test_external_cooldown_expires_at_48h_but_suppression_does_not(box, mail, now, tmp_path):
    box.reconcile_external(mail.account, [record(mail, now)], "b" * 64, now)
    later = now + timedelta(hours=48)
    assert exclusions(tmp_path / "outbox.sqlite3", later) == (set(), set())
    box.prepare("batch", [mail], later, recipients=[mail.recipient])
    with pytest.raises(DeliveryBlocked, match="suppressed"):
        box.reserve("batch", mail, [], later, 500)


def test_conflicting_reimport_rolls_back_whole_batch(box, mail, now):
    first = record(mail, now)
    box.reconcile_external(mail.account, [first], "b" * 64, now)
    conflicting = {**first, "recipient": "wrong@company.example"}
    with pytest.raises(ValueError, match="conflict"):
        box.reconcile_external(
            mail.account, [{**first, "id": "external-2"}, conflicting], "b" * 64, now
        )
    assert box.status()["external_sent"] == 1


def test_hold_preserves_history_and_blocks_stale_send_or_draft(box, mail, now):
    box.prepare("old", [mail], now)
    assert box.hold_batch("old", now) == 1
    assert box.hold_batch("old", now) == 0
    assert box.pending("old") == box.pending_drafts("old") == []
    assert box.status()["states"] == {"prepared": 1}
    assert box.status()["held_prepared"] == 1
    with pytest.raises(DeliveryBlocked, match="held"):
        box.reserve("old", mail, [], now, 500)
    with pytest.raises(DeliveryBlocked, match="held"):
        box.reserve_draft("old", mail, now)
    with pytest.raises(ValueError):
        box.hold_batch("missing", now)


def test_invalid_external_and_managed_conflicts_are_not_imported(box, mail, now):
    with pytest.raises(ValueError):
        box.reconcile_external(mail.account, [], "b" * 64, now)
    with pytest.raises(ValueError):
        box.reconcile_external(
            mail.account, [{**record(mail, now), "company_key": "bad"}], "b" * 64, now
        )
    box.prepare("old", [mail], now)
    box.reserve("old", mail, [], now, 500)
    box.unknown("old", mail, "external-1", now)
    with pytest.raises(ValueError, match="Managed"):
        box.reconcile_external(mail.account, [record(mail, now)], "b" * 64, now)
    with pytest.raises(ValueError):
        box.hold_batch("old", now)


def test_history_conflict_and_independent_accounts(box, mail, now):
    box.connection.execute(
        "INSERT INTO history VALUES (?,?,?,?)",
        (mail.account, "external-1", now.isoformat(), json.dumps(["different@example.com"])),
    )
    with pytest.raises(ValueError, match="History"):
        box.reconcile_external(mail.account, [record(mail, now)], "b" * 64, now)
    assert box.reconcile_external("another@example.com", [record(mail, now)], "b" * 64, now) == 1


def test_hold_mixed_batch_preserves_already_sent_rows(box, mail, now):
    other = replace(mail, recipient="other@different.example", company_key="different.example")
    box.prepare("mixed", [mail, other], now)
    box.reserve("mixed", mail, [], now, 500)
    box.finish("mixed", mail, Receipt("managed", now), now)
    assert box.hold_batch("mixed", now) == 1
    assert box.status()["states"] == {"prepared": 1, "sent": 1}
    assert box.pending("mixed") == []
