import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from job_radar.adapters.outbound.bridge_errors import ERROR_CODES, safe_error, safe_phase
from job_radar.adapters.outbound.gmail_bridge import FileConnector, GmailBridge
from job_radar.domain.mail import DeliveryUnknown
from scripts.chrome_bridge import Bridge, handler, normalize, original


@pytest.fixture
def record():
    message = EmailMessage()
    message["From"] = "Synthetic Sender <sender@example.com>"
    message["To"] = "Hiring <jobs@example.com>"
    message["Subject"] = "Application"
    message["Date"] = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    message["Message-ID"] = "<test-123@example.com>"
    message.set_content("Hello, synthetic recipient.\n")
    message.add_attachment(b"%PDF-test", maintype="application", subtype="pdf", filename="CV.pdf")
    return {"id": "abcdef123456", "labels": ["SENT"], "raw": message.as_string()}


def enqueue(bridge, *, method="gmail_get_profile", arguments=None, expires=None, identifier=None):
    identifier = identifier or "a" * 32
    folder = bridge.directory / ("b" * 32)
    folder.mkdir(exist_ok=True)
    path = folder / f"{identifier}.request.json"
    data = {
        "id": identifier,
        "method": method,
        "arguments": arguments or {},
        "expires_at": expires if expires is not None else time.time() + 60,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def response(path):
    return json.loads(path.with_name(path.name.replace("request", "response")).read_text())


def test_original_normalizes_real_mime_and_attachment_hash(record):
    result = original(record)
    assert result["to"] == ["jobs@example.com"]
    assert result["timestamp_source"] == "rfc822_date"
    assert result["email_ts"] == "2026-01-02T03:04:00+00:00"
    attachment = result["payload"]["parts"][1]
    assert attachment["body"] == {"size": 9, "sha256": hashlib.sha256(b"%PDF-test").hexdigest()}
    assert "base64" not in json.dumps(result)
    assert result["payload"]["headers"][0]["value"] == "sender@example.com"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../../file"},
        {"raw": "not mime"},
        {"labels": ["OTHER"]},
        {"labels": "SENT"},
    ],
)
def test_rejects_invalid_original(record, change):
    with pytest.raises(ValueError):
        original({**record, **change})


def test_search_requires_complete_history_and_drops_raw_mime(record):
    result = normalize("gmail_search_emails", {"complete": True, "emails": [record]})
    assert result["emails"][0]["labels"] == ["SENT"]
    assert "raw" not in json.dumps(result)
    with pytest.raises(ValueError, match="incomplete_history"):
        normalize("gmail_search_emails", {"emails": []})
    result = normalize(
        "gmail_search_emails",
        {
            "complete": True,
            "emails": [
                {"id": "draft", "labels": ["DRAFT"]},
            ],
        },
    )
    assert result["emails"][0]["labels"] == ["DRAFT"]


@pytest.mark.parametrize(
    "method,result",
    [
        ("gmail_get_profile", {"email": "invalid"}),
        ("gmail_send_email", {"id": "invented-id"}),
        ("gmail_create_draft", {"id": "invented-id"}),
    ],
)
def test_invalid_provider_result(method, result):
    with pytest.raises(ValueError):
        normalize(method, result)


def test_origin_and_token_are_both_required_and_origin_is_pinned(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    origin = "chrome-extension://" + "a" * 32
    assert not bridge.authorized("https://evil.example", bridge.token)
    assert not bridge.authorized(origin, "wrong")
    assert bridge.authorized(origin, bridge.token)
    assert not bridge.authorized("chrome-extension://" + "b" * 32, bridge.token)


def test_claim_single_dispatch_and_durable_response(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, data = enqueue(bridge)
    assert bridge.take()["request"] == data
    assert bridge.take() == {"busy": True}
    assert path.with_name(data["id"] + ".dispatched").exists()
    result = bridge.finish({"id": data["id"], "result": {"email": "sender@example.com"}})
    assert not result["halted"]
    assert response(path)["result"]["email"] == "sender@example.com"
    assert bridge.take() == {"idle": True}
    with pytest.raises(ValueError, match="unexpected_response"):
        bridge.finish({"id": data["id"], "result": {}})


def test_crash_claim_is_not_replayed(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, _ = enqueue(bridge)
    bridge.take()
    restarted = Bridge(tmp_path, "sender@example.com")
    assert restarted.take() == {"halted": True}
    assert response(path)["error"] == "previous_dispatch_unknown"
    assert restarted.take() == {"halted": True}


def test_expiration_during_dispatch_halts(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, _ = enqueue(bridge)
    bridge.take()
    bridge.active[1]["expires_at"] = 0
    assert bridge.take() == {"halted": True}
    assert response(path)["error"] == "expired_unknown"


@pytest.mark.parametrize(
    "enabled,account", [(False, "sender@example.com"), (True, "other@example.com")]
)
def test_send_requires_explicit_enable_and_matching_account(tmp_path, enabled, account):
    bridge = Bridge(tmp_path, "sender@example.com", enable_send=enabled)
    path, _ = enqueue(bridge, method="gmail_send_email", arguments={"from_address": account})
    assert bridge.take() == {"idle": True}
    assert response(path)["error"] == "send_not_authorized"


def test_draft_requires_matching_account_but_not_send_enable(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com", enable_send=False)
    path, data = enqueue(
        bridge,
        method="gmail_create_draft",
        arguments={"from_address": "sender@example.com"},
    )
    assert bridge.take()["request"] == data
    result = bridge.finish({"id": data["id"], "result": {"id": "abcdef123456"}})
    assert not result["halted"]
    assert response(path)["result"] == {"id": "abcdef123456"}


@pytest.mark.parametrize(
    "result", [{"error": "ui_timeout"}, {"result": {"email": "other@example.com"}}]
)
def test_browser_failure_or_wrong_account_halts(tmp_path, result):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, data = enqueue(bridge)
    bridge.take()
    assert bridge.finish({"id": data["id"], **result})["halted"]
    assert response(path)["error"] == "browser_result_invalid"


@pytest.mark.parametrize(
    "change", [{"method": "eval"}, {"id": "bad"}, {"expires_at": float("nan")}, {"expires_at": 0}]
)
def test_invalid_and_expired_requests_do_not_dispatch(tmp_path, change):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, data = enqueue(bridge)
    path.write_text(json.dumps({**data, **change}))
    assert bridge.take() == {"idle": True}
    assert "error" in response(path)


def test_http_auth_host_body_and_preflight(tmp_path, capsys):
    bridge = Bridge(tmp_path, "sender@example.com")
    with ThreadingHTTPServer(("127.0.0.1", 0), handler(bridge)) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            origin = "chrome-extension://" + "a" * 32
            headers = {"Origin": origin, "Authorization": "Bearer " + bridge.token}
            for method, path, body, extra, expected in [
                ("POST", "/next", "{}", {}, 200),
                ("POST", "/next", '{"worker_version":"0.1.3"}', {}, 200),
                ("POST", "/next", '{"worker_version":"0.1.3"}', {}, 200),
                ("POST", "/next", '{"worker_version":"private@example.com"}', {}, 200),
                ("POST", "/next", "{}", {"Authorization": "bad"}, 403),
                ("POST", "/next", "{}", {"Origin": "https://evil.example"}, 403),
                ("POST", "/next", "{}", {"Host": "evil.example"}, 403),
                ("POST", "/next", "[]", {}, 400),
                ("POST", "/next", "bad", {}, 400),
                ("POST", "/other", "{}", {}, 404),
                ("OPTIONS", "/next", "", {}, 204),
                ("OPTIONS", "/next", "", {"Origin": "https://evil.example"}, 403),
            ]:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request(method, path, body, headers | extra)
                    received = connection.getresponse()
                    assert received.status == expected
                    received.read()
                finally:
                    connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=3)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records == [
        {"event": "worker_connected", "version": version, "sending": False}
        for version in ["unknown", "0.1.3", "unknown"]
    ]


def test_existing_file_connector_completes_without_assistant(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    connector = FileConnector(tmp_path, timeout=3)
    done = threading.Event()

    def worker():
        while not done.wait(0.01):
            command = bridge.take()
            if "request" in command:
                bridge.finish(
                    {"id": command["request"]["id"], "result": {"email": "sender@example.com"}}
                )
                return

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert GmailBridge(connector).profile() == "sender@example.com"
    finally:
        done.set()
        thread.join(timeout=3)


def test_attachment_hash_mismatch_fails_readback(record, mail):
    candidate = mail
    normalized = original(record)
    normalized["payload"]["headers"] = [
        {"name": "From", "value": candidate.account},
        {"name": "To", "value": candidate.recipient},
        {"name": "Subject", "value": candidate.subject},
    ]
    normalized["payload"]["parts"][0]["body"]["content"] = candidate.body
    attachment = normalized["payload"]["parts"][1]
    attachment["body"]["size"] = candidate.attachment_size
    attachment["filename"] = candidate.attachment_name

    class Connector:
        def call(self, method, arguments):
            return normalized

    with pytest.raises(DeliveryUnknown, match="verification_mismatch"):
        GmailBridge(Connector()).verify(candidate, record["id"])


def test_search_error_survives_bridge_and_file_connector(tmp_path):
    bridge = Bridge(tmp_path, "sender@example.com")
    connector = FileConnector(tmp_path, timeout=3)
    done = threading.Event()

    def worker():
        while not done.wait(0.01):
            command = bridge.take()
            if "request" in command:
                bridge.finish(
                    {
                        "id": command["request"]["id"],
                        "error": "unknown_pagination",
                        "phase": "search",
                    }
                )
                return

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        with pytest.raises(DeliveryUnknown, match="connector_unknown_pagination"):
            GmailBridge(connector).search("in:sent after:123")
    finally:
        done.set()
        thread.join(timeout=3)


def test_extension_and_python_share_error_allowlist():
    text = (Path(__file__).resolve().parents[1] / "chrome-extension/protocol.mjs").read_text()
    definitions = text.split("export const ERROR_CODES = new Set([", 1)[1].split("]);", 1)[0]
    assert set(re.findall(r"'([^']+)'", definitions)) == ERROR_CODES


@pytest.mark.parametrize("value", [None, {}, 5, "secret@example.com token=secret"])
def test_error_and_phase_sanitization(value):
    assert safe_error(value) == "browser_operation_failed"
    assert safe_phase(value) == "dispatch"


def test_safe_error_codes_are_preserved():
    assert safe_error("unknown_pagination") == "unknown_pagination"
    assert safe_phase("page") == "page"


def test_durable_error_and_log_are_sanitized(tmp_path, capsys):
    bridge = Bridge(tmp_path, "sender@example.com")
    path, data = enqueue(bridge, method="gmail_search_emails")
    bridge.take()
    bridge.finish({"id": data["id"], "error": "secret@example.com token=secret", "phase": "secret"})
    stored = response(path)
    assert stored["error_code"] == "browser_operation_failed"
    assert stored["phase"] == "dispatch"
    output = capsys.readouterr().out
    assert "secret" not in json.dumps(stored) + output
    assert json.loads(output)["error_code"] == "browser_operation_failed"


@pytest.mark.parametrize("history", [False, True])
def test_probe_can_check_history_without_sending(tmp_path, monkeypatch, capsys, history):
    from scripts import chrome_probe

    class Gateway:
        def profile(self):
            return "sender@example.com"

        def recent_sent(self, since):
            assert (datetime.now(UTC) - since).total_seconds() == pytest.approx(172800, abs=1)
            return [object(), object()]

    monkeypatch.setattr(chrome_probe, "GmailBridge", lambda connector: Gateway())
    assert (
        chrome_probe.main(
            ["--account", "sender@example.com", "--bridge-dir", str(tmp_path)]
            + (["--history"] if history else [])
        )
        == 0
    )
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(row["sent"] == row["llm_calls"] == 0 for row in rows)
    if history:
        assert rows[-1]["history_complete"] and rows[-1]["messages"] == 2


def test_probe_error_is_compact_and_never_pretends_complete(tmp_path, monkeypatch, capsys):
    from scripts import chrome_probe

    class Gateway:
        def profile(self):
            return "sender@example.com"

        def recent_sent(self, since):
            raise DeliveryUnknown("connector_unknown_pagination")

    monkeypatch.setattr(chrome_probe, "GmailBridge", lambda connector: Gateway())
    assert (
        chrome_probe.main(
            ["--account", "sender@example.com", "--history", "--bridge-dir", str(tmp_path)]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == {
        "stage": "history",
        "error_code": "connector_unknown_pagination",
        "sent": 0,
        "llm_calls": 0,
    }
