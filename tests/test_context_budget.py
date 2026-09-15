import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_radar.adapters.outbound.gmail_native import SCOPES, inspect_credentials
from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.adapters.outbound.research import EvidenceCache
from job_radar.adapters.outbound.review import exclusions, review_packet
from job_radar.application.collection import ResearchRequest, ResearchResponse
from job_radar.bootstrap import main


def evidence(tmp_path: Path, count: int = 3) -> Path:
    path = tmp_path / "research" / "evidence.sqlite3"
    cache = EvidenceCache(path)
    for n in range(count):
        cache.put(
            ResearchRequest("web", f"https://team{n}.example/jobs"),
            ResearchResponse(
                "ok",
                f"Title: DevOps Engineer {n}\nMarkdown Content:\n"
                + "Navigation and repeated decoration. " * 1000
                + f"\n# DevOps Engineer\nRemote worldwide. Salary undisclosed.\n"
                f"Send your CV to jobs@team{n}.example\n"
                "## More relevant posts\nPrivacy privacy@unrelated.example",
            ),
        )
    cache.close()
    return path.parent


def test_review_has_a_hard_context_budget_and_continuation(tmp_path, capsys):
    directory = evidence(tmp_path)
    args = ["review", "--data-dir", str(directory), "--limit", "1", "--max-chars", "2500"]
    cursor = 0
    snapshot = []
    seen = []
    for _ in range(3):
        assert main([*args, "--after", str(cursor), *snapshot]) == 0
        output = capsys.readouterr().out
        assert len(output.rstrip()) <= 2500
        packet = json.loads(output)
        assert packet["network_enabled"] is False and packet["llm_calls"] == 0
        card = packet["items"][0]
        assert card["requires_review"] is True
        assert card["official_application_contact"] is False
        assert "privacy@unrelated.example" not in output
        assert "Navigation and repeated" not in output
        seen.append(card["id"])
        assert packet["next_cursor"] > cursor
        cursor = packet["next_cursor"]
        snapshot = ["--snapshot", packet["snapshot"]]
    assert len(set(seen)) == 3
    assert main([*args, "--after", str(cursor), *snapshot]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []


def test_review_saved_packet_prints_only_counts_and_does_not_overwrite(tmp_path, capsys):
    directory = evidence(tmp_path, 1)
    target = tmp_path / "packet.json"
    args = ["review", "--data-dir", str(directory), "--output", str(target)]
    assert main(args) == 0
    assert "jobs@" not in capsys.readouterr().out
    assert len(json.loads(target.read_text())["items"]) == 1
    original = hashlib.sha256(target.read_bytes()).hexdigest()
    assert main(args) == 3
    assert hashlib.sha256(target.read_bytes()).hexdigest() == original


def test_mail_check_detects_missing_oauth_without_network_or_outbox(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Readiness check must not connect or create an outbox")

    monkeypatch.setattr("job_radar.bootstrap.connect", forbidden)
    monkeypatch.setattr("job_radar.bootstrap.SQLiteOutbox", forbidden)
    assert main(["mail", "check", "--credentials", str(tmp_path / "missing.json")]) == 6
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "oauth_setup_required"
    assert report["network_enabled"] is False
    assert report["provider_verified"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 101},
        {"max_chars": 2047},
        {"max_chars": 64001},
        {"after": -1},
        {"after": 1},
    ],
)
def test_invalid_review_budgets(tmp_path, options):
    with pytest.raises(ValueError):
        review_packet(tmp_path / "never-created.db", **options)
    assert not list(tmp_path.iterdir())


def test_budget_stops_without_losing_next_record(tmp_path):
    directory = evidence(tmp_path, 5)
    first = review_packet(directory / "evidence.sqlite3", max_chars=2048)
    assert first["has_more"] and 0 < len(first["items"]) < 5
    second = review_packet(
        directory / "evidence.sqlite3", after=first["next_cursor"], snapshot=first["snapshot"]
    )
    assert len(second["items"]) + len(first["items"]) == 5
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert not second["has_more"]


def test_seen_packets_skip_same_digest_but_changed_evidence_is_reviewed(tmp_path):
    directory = evidence(tmp_path, 1)
    path = directory / "evidence.sqlite3"
    first = review_packet(path)
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps(first))
    assert review_packet(path, seen=(seen,))["items"] == []
    cache = EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://team0.example/jobs"),
        ResearchResponse("ok", "# Changed role\nSend CV to updated@team0.example"),
    )
    cache.close()
    assert review_packet(path, seen=(seen,))["items"][0]["title"] == "Changed role"
    with pytest.raises(ValueError, match="Cache changed"):
        review_packet(path, after=first["next_cursor"], snapshot=first["snapshot"])


def test_no_contact_pages_and_failed_reads_are_not_exported(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    cache = EvidenceCache(path)
    for n, (kind, status, body) in enumerate(
        [
            ("web", "ok", "No contacts"),
            ("web", "reader_failed", "jobs@failed.example"),
            ("search", "ok", "jobs@search.example"),
        ]
    ):
        cache.put(ResearchRequest(kind, f"https://team{n}.example"), ResearchResponse(status, body))
    cache.close()
    packet = review_packet(path)
    assert packet["items"] == [] and packet["scanned"] == packet["skipped"] == 1


def test_long_single_card_is_not_silently_truncated(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    cache = EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://team.example/" + "x" * 2500),
        ResearchResponse("ok", "jobs@team.example"),
    )
    cache.close()
    with pytest.raises(ValueError, match="One evidence"):
        review_packet(path, max_chars=2048)


def test_contact_lists_are_bounded_and_untrusted(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    cache = EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://team.example"),
        ResearchResponse("ok", " ".join(f"jobs{n}@team.example" for n in range(20))),
    )
    cache.close()
    item = review_packet(path)["items"][0]
    assert len(item["contacts"]) == 8 and item["contacts_truncated"]
    assert item["title"] == "Untitled evidence"
    assert not item["suppression_reviewed"]


def test_outbox_filters_48h_not_all_history_and_keeps_suppressions(tmp_path):
    directory = evidence(tmp_path, 5)
    path = tmp_path / "outbox.sqlite3"
    db = SQLiteOutbox(path)
    now = datetime.now(UTC)
    db.connection.execute("INSERT INTO batches VALUES ('b', ?)", (now.isoformat(),))
    for n, state, age in [(0, "sent", 1), (1, "sent", 49), (2, "unknown", 1)]:
        db.connection.execute(
            "INSERT INTO outbox VALUES ('b',?,'me@example.com',?,?,?, ?,NULL,?)",
            (
                str(n),
                f"jobs@team{n}.example",
                f"team{n}.example",
                "{}",
                state,
                (now - timedelta(hours=age)).isoformat(),
            ),
        )
    db.connection.execute(
        "INSERT INTO suppressions VALUES ('me@example.com','company','team3.example')"
    )
    db.connection.execute(
        "INSERT INTO suppressions VALUES ('me@example.com','recipient','jobs@team4.example')"
    )
    db.close()
    packet = review_packet(directory / "evidence.sqlite3", outbox=path, now=now)
    assert len(packet["items"]) == 1
    assert packet["items"][0]["contacts"] == ["jobs@team1.example"]
    assert exclusions(None, now) == (set(), set())


@pytest.mark.parametrize(
    "value,expected",
    [
        ("not json", "unreadable_credentials"),
        ("{}", "incomplete_credentials"),
        ("[]", "incomplete_credentials"),
        ("x" * 65537, "unreadable_credentials"),
    ],
    ids=["invalid-json", "empty-object", "wrong-type", "oversized"],
)
def test_invalid_credentials_are_redacted(tmp_path, value, expected):
    path = tmp_path / "token.json"
    path.write_text(value)
    report = inspect_credentials(path)
    assert report["reason"] == expected and not report["local_credentials_configured"]
    assert value not in json.dumps(report) or value in ("{}", "[]")


def test_configured_oauth_is_not_claimed_as_verified(tmp_path, capsys):
    path = tmp_path / "token.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "PRIVATE_ID",
                "client_secret": "PRIVATE_SECRET",
                "refresh_token": "PRIVATE_TOKEN",
                "scopes": SCOPES,
            }
        )
    )
    assert main(["mail", "check", "--credentials", str(path)]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    report = json.loads(output)
    assert report["local_credentials_configured"] and not report["provider_verified"]


def test_missing_review_database_is_not_created(tmp_path, capsys):
    assert main(["review", "--data-dir", str(tmp_path)]) == 4
    assert "storage_or_runtime_error" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
