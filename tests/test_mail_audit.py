import json

import pytest

from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.bootstrap import main
from job_radar.domain.mail import Receipt


@pytest.fixture
def outbox(tmp_path):
    ledger = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    try:
        yield ledger
    finally:
        ledger.close()


def audit(tmp_path, capsys, *, ids=None, account="sender@example.com"):
    source = tmp_path / "provider-ids.json"
    source.write_text(json.dumps({"account": account, "message_ids": ids or ["verified-id"]}))
    result = main(["mail", "audit", "--input", str(source), "--data-dir", str(tmp_path)])
    return result, json.loads(capsys.readouterr().out)


def test_missing_ledger_is_not_zero_sends_or_success(tmp_path, capsys):
    code, report = audit(tmp_path, capsys)
    assert code == 7
    assert report["untracked"] == 1 and report["ledger_sent"] == 0
    assert report["initialized"] is False
    assert not (tmp_path / "outbox.sqlite3").exists()
    assert report["valid_applications"] is None


def test_audit_matches_account_ids_and_does_not_mutate(tmp_path, capsys, mail, now, outbox):
    outbox.prepare("batch", [mail], now)
    outbox.reserve("batch", mail, [], now, 500)
    outbox.finish("batch", mail, Receipt("verified-id", now), now)
    changes = outbox.connection.execute("SELECT count(*) FROM mail_audit").fetchone()[0]
    code, report = audit(tmp_path, capsys, ids=["verified-id", "verified-id"])
    assert code == 0 and report["ledger_sent"] == 1
    assert report["duplicate_input_ids"] == 1
    assert "sender@example.com" not in json.dumps(report)
    assert "verified-id" not in json.dumps(report)
    assert audit(tmp_path, capsys, account="different@example.com")[1]["untracked"] == 1
    assert outbox.connection.execute("SELECT count(*) FROM mail_audit").fetchone()[0] == changes


def test_uncertain_state_is_not_confirmed_sent(tmp_path, capsys, mail, now, outbox):
    outbox.prepare("batch", [mail], now)
    outbox.reserve("batch", mail, [], now, 500)
    outbox.unknown("batch", mail, "verified-id", now)
    code, report = audit(tmp_path, capsys)
    assert code == 7 and report["ledger_not_sent"] == 1
    assert report["ledger_sent"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"account": "bad", "message_ids": ["id"]},
        {"account": "sender@example.com", "message_ids": []},
        {"account": "sender@example.com", "message_ids": ["id"] * 501},
        {"account": "sender@example.com", "message_ids": [""]},
    ],
)
def test_invalid_audit_input_fails_closed(tmp_path, capsys, payload):
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(payload))
    assert main(["mail", "audit", "--input", str(source), "--data-dir", str(tmp_path)]) != 0
    capsys.readouterr()
    assert not (tmp_path / "outbox.sqlite3").exists()


def test_oversized_input_is_rejected_without_creating_ledger(tmp_path, capsys):
    source = tmp_path / "oversized.json"
    source.write_bytes(b" " * 1_000_001)
    assert main(["mail", "audit", "--input", str(source), "--data-dir", str(tmp_path)]) == 2
    capsys.readouterr()
    assert not (tmp_path / "outbox.sqlite3").exists()
