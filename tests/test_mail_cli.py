import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from test_delivery import Gateway

from job_radar.adapters.outbound.mail_plan import prepare_plan
from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.bootstrap import main
from job_radar.domain.mail import DeliveryUnknown

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "case,ready",
    [
        ("valid", True),
        ("missing", False),
        ("tampered", False),
        ("instructions", False),
        ("reviewed", True),
        ("closed", False),
    ],
)
def test_hn_requires_fresh_primary_evidence_and_application_review(plan_files, case, ready):
    path, config, manifest = plan_files
    item = manifest["candidates"][0]
    url = "https://news.ycombinator.com/item?id=123"
    text = "Markdown Content:\nExample | DevOps Engineer | Remote\nApply jobs@company.example"
    if case in {"instructions", "reviewed"}:
        text += "\nUse subject HN Engineer."
    if case == "closed":
        text += "\nThis position is closed."
    item["job"].update(url=url, description=text)
    item["contact_source_url"] = url
    if case != "missing":
        item["primary_evidence"] = {
            "url": url,
            "text": text,
            "sha256": sha256(text.encode()).hexdigest(),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    if case == "tampered":
        item["primary_evidence"]["sha256"] = "0" * 64
    item["application_requirements_reviewed"] = case in {"reviewed", "closed"}
    path.write_text(json.dumps(manifest))
    _, mails, _, _ = prepare_plan(path, config)
    assert bool(mails) == ready
    if ready:
        original_batch = prepare_plan(path, config)[0]
        proof = item["primary_evidence"]
        proof["text"] += "\nAdditional source context."
        proof["sha256"] = sha256(proof["text"].encode()).hexdigest()
        path.write_text(json.dumps(manifest))
        assert prepare_plan(path, config)[0] != original_batch


def test_cli_hold_requires_exact_batch(plan_files, tmp_path, capsys):
    path, config, _ = plan_files
    batch, mails, _, _ = prepare_plan(path, config)
    box = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    box.prepare(batch, mails, datetime.now(UTC))
    box.close()
    args = ["mail", "hold", "--batch", batch, "--data-dir", str(tmp_path)]
    assert main(args + ["--confirm", "wrong"]) == 2
    capsys.readouterr()
    assert main(args + ["--confirm", batch]) == 0
    assert json.loads(capsys.readouterr().out)["held"] == 1


@pytest.mark.parametrize(
    "original,ready",
    [
        ("Send CV to [firstname]@company.example", False),
        ("Send CV to jobs@company.example", True),
        ("Send CV to JOBS@company.example", True),
        ("Send CV to other-jobs@company.example", False),
        ("Apply on our site.\n## Related postings\nEmail jobs@company.example", False),
    ],
)
def test_aggregator_contact_requires_literal_original_evidence(plan_files, original, ready):
    path, config, manifest = plan_files
    item = manifest["candidates"][0]
    item["contact_source_url"] = item["job"]["url"]
    item["job"]["description"] = "Apply via jobs@company.example\n## Original posting\n" + original
    path.write_text(json.dumps(manifest), encoding="utf-8")
    _, mails, counts, _ = prepare_plan(path, config)
    assert len(mails) == int(ready)
    assert counts["review"] == int(not ready)


def test_manifest_is_stable_and_content_changes_authorization(plan_files):
    path, config, manifest = plan_files
    batch, mails, counts, _ = prepare_plan(path, config)
    assert counts == {"ready": 1, "review": 0, "rejected": 0}
    assert prepare_plan(path, config)[0] == batch
    assert mails[0].attachment_size == 14
    manifest["candidates"][0]["body"] += " Modified"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert prepare_plan(path, config)[0] != batch


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"official_application_contact": False}, "review"),
        ({"suppression_reviewed": False}, "review"),
        ({"salary_min_monthly_usd": 1000, "salary_max_monthly_usd": 1800}, "rejected"),
        ({"salary_disclosed": True}, "review"),
    ],
)
def test_manifest_guards(plan_files, change, expected):
    path, config, manifest = plan_files
    item = manifest["candidates"][0]
    if any(key.startswith("salary") for key in change):
        item["job"].update(change)
    else:
        item.update(change)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    _, mails, counts, _ = prepare_plan(path, config)
    assert mails == [] and counts[expected] == 1


@pytest.mark.parametrize("oversized", [False, True])
def test_invalid_cv(plan_files, oversized):
    path, config, _ = plan_files
    bad_cv = b"%PDF-" + b"x" * 2_000_000 if oversized else b"not a PDF"
    (path.parent / "cv.pdf").write_bytes(bad_cv)
    with pytest.raises(ValueError):
        prepare_plan(path, config)


def test_unregistered_source(plan_files):
    path, config, manifest = plan_files
    manifest["candidates"][0]["job"]["source_id"] = "unregistered"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_plan(path, config)


def test_cli_prepare_and_run(plan_files, tmp_path, capsys, monkeypatch):
    path, config, _ = plan_files
    state = tmp_path / "state"
    assert main(["mail", "status", "--data-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["initialized"] is False
    assert not state.exists()
    base = ["--config", str(config), "mail"]
    args = ["--data-dir", str(state)]
    assert main(base + ["prepare", "--manifest", str(path), *args]) == 0
    result = json.loads(capsys.readouterr().out)
    batch, mails, _, _ = prepare_plan(path, config)
    assert result["batch"] == batch
    gateway = Gateway(mails[0])
    monkeypatch.setattr("job_radar.bootstrap.GmailBridge", lambda connector: gateway)
    run = base + [
        "run",
        "--batch",
        batch,
        "--confirm",
        batch,
        "--bridge-dir",
        str(tmp_path / "bridge"),
        *args,
    ]
    assert main(run) == 0
    assert json.loads(capsys.readouterr().out)["sent"] == 1
    assert main(base + ["status", *args]) == 0
    assert json.loads(capsys.readouterr().out)["states"] == {"sent": 1}
    assert main(run) == 0
    assert json.loads(capsys.readouterr().out)["sent"] == 0
    assert len(gateway.sent) == 1


def test_cli_creates_a_draft_without_sending(plan_files, tmp_path, capsys, monkeypatch):
    path, config, _ = plan_files
    state = tmp_path / "state"
    base = ["--config", str(config), "mail"]
    assert main(base + ["prepare", "--manifest", str(path), "--data-dir", str(state)]) == 0
    batch, mails, _, _ = prepare_plan(path, config)
    gateway = Gateway(mails[0])
    monkeypatch.setattr("job_radar.bootstrap.GmailBridge", lambda connector: gateway)
    command = base + [
        "draft",
        "--batch",
        batch,
        "--confirm",
        batch,
        "--bridge-dir",
        str(tmp_path / "bridge"),
        "--data-dir",
        str(state),
    ]
    assert main(command) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["drafted"] == 1
    assert gateway.sent == []
    assert gateway.drafts == mails


@pytest.mark.parametrize(
    "case,expected", [("confirmation", 2), ("quota", 2), ("account", 6), ("unknown", 5)]
)
def test_cli_send_failures(plan_files, tmp_path, monkeypatch, capsys, case, expected):
    path, config, _ = plan_files
    batch, mails, _, _ = prepare_plan(path, config)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.prepare(batch, mails, datetime.now(UTC))
    outbox.close()
    gateway = Gateway(mails[0])
    if case == "account":
        gateway.account = "different@example.com"
    elif case == "unknown":
        gateway.failure = "verify"
    monkeypatch.setattr("job_radar.bootstrap.GmailBridge", lambda connector: gateway)
    args = [
        "mail",
        "run",
        "--batch",
        batch,
        "--confirm",
        "wrong" if case == "confirmation" else batch,
        "--daily-limit",
        "501" if case == "quota" else "500",
        "--data-dir",
        str(tmp_path),
        "--bridge-dir",
        str(tmp_path / "bridge"),
    ]
    assert main(args) == expected
    output = json.loads(capsys.readouterr().out)
    assert mails[0].recipient not in str(output)


@pytest.mark.parametrize("case", ["success", "missing_id", "wrong_account", "read_failure"])
def test_cli_reconciliation(plan_files, tmp_path, monkeypatch, capsys, case):
    path, config, _ = plan_files
    batch, mails, _, _ = prepare_plan(path, config)
    mail = mails[0]
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    now = datetime.now(UTC)
    outbox.prepare(batch, mails, now)
    outbox.reserve(batch, mail, [], now, 100)
    outbox.unknown(batch, mail, None if case == "missing_id" else "provider-1", now)
    outbox.close()
    gateway = Gateway(mail)
    if case == "wrong_account":
        gateway.account = "different@example.com"
    elif case == "read_failure":

        def fail(*args):
            raise DeliveryUnknown("test")

        gateway.verify = fail
    monkeypatch.setattr("job_radar.bootstrap.GmailBridge", lambda connector: gateway)
    args = [
        "mail",
        "reconcile",
        "--batch",
        batch,
        "--mail-id",
        mail.key,
        "--data-dir",
        str(tmp_path),
        "--bridge-dir",
        str(tmp_path / "bridge"),
    ]
    expected = {"success": 0, "missing_id": 2, "wrong_account": 6, "read_failure": 5}[case]
    assert main(args) == expected
    output = json.loads(capsys.readouterr().out)
    assert output.get("reconciled") is (True if case == "success" else None)
