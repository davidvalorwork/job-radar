import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from job_radar.adapters.outbound.external_receipts import external_plan
from job_radar.bootstrap import main


@pytest.fixture
def export_file(tmp_path):
    now = datetime.now(UTC)
    data = {
        "account": "sender@example.com",
        "observed_at": now.isoformat(),
        "expected": [
            {
                "id": "one",
                "recipient": "jobs@company.example",
                "company_key": "company.example",
                "subject": "Application",
            }
        ],
        "messages": [
            {
                "id": "one",
                "label_ids": ["SENT"],
                "internal_date": str(int(now.timestamp() * 1000)),
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.com>"},
                    {"name": "To", "value": "jobs@company.example"},
                    {"name": "Subject", "value": "Application"},
                ],
            }
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(data))
    return path, data, now


def test_cli_preview_confirmation_and_repeat(export_file, tmp_path, capsys):
    path, _, _ = export_file
    directory = tmp_path / "ledger"
    args = ["mail", "reconcile-external", "--input", str(path), "--data-dir", str(directory)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert not directory.exists()
    assert main(args + ["--confirm", "wrong"]) == 2
    capsys.readouterr()
    assert not directory.exists()
    assert main(args + ["--confirm", report["confirmation"]]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 1
    assert main(args + ["--confirm", report["confirmation"]]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "wrong_from",
        "wrong_to",
        "wrong_subject",
        "cc",
        "bcc",
        "missing_header",
        "duplicate_header",
        "draft",
        "not_sent",
        "missing_id",
        "duplicate_id",
        "duplicate_expected",
        "future",
        "old_export",
        "future_export",
        "overflow",
    ],
)
def test_readback_fails_closed(export_file, change):
    path, data, now = export_file
    item = data["messages"][0]
    if change.startswith("wrong_"):
        name = change.removeprefix("wrong_")
        next(h for h in item["headers"] if h["name"].lower() == name)["value"] = "wrong@example.com"
    elif change in {"cc", "bcc"}:
        item["headers"].append({"name": change, "value": "extra@example.com"})
    elif change == "missing_header":
        item["headers"].pop()
    elif change == "duplicate_header":
        item["headers"].append(deepcopy(item["headers"][0]))
    elif change == "draft":
        item["label_ids"].append("DRAFT")
    elif change == "not_sent":
        item["label_ids"] = []
    elif change == "missing_id":
        item["id"] = "unexpected"
    elif change == "duplicate_id":
        data["messages"].append(deepcopy(item))
    elif change == "duplicate_expected":
        data["expected"].append(deepcopy(data["expected"][0]))
    elif change == "future":
        item["internal_date"] = str(int((now + timedelta(days=1)).timestamp() * 1000))
    elif change == "old_export":
        data["observed_at"] = (now - timedelta(days=2)).isoformat()
    elif change == "future_export":
        data["observed_at"] = (now + timedelta(days=1)).isoformat()
    else:
        item["internal_date"] = "999999999999999"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        external_plan(path, now)


def test_export_byte_limit(export_file):
    path, _, now = export_file
    path.write_bytes(b" " * 4_000_001)
    with pytest.raises(ValueError, match="exceeds"):
        external_plan(path, now)
