import base64
import json
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from job_radar.adapters.outbound import gmail_native as native
from job_radar.adapters.outbound.gmail_bridge import GmailBridge
from job_radar.domain.mail import DeliveryBlocked, DeliveryUnknown


class Service:
    def __init__(self, records=()):
        self.records = {r["id"]: r for r in records}
        self.batches = []
        self.sent = []
        self.fail = False
        self.closed = False

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, **kwargs):
        return SimpleNamespace(execute=lambda **_: {"emailAddress": "sender@example.com"})

    def list(self, **kwargs):
        return SimpleNamespace(execute=lambda **_: {"messages": [{"id": i} for i in self.records]})

    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda **_: self.records[kwargs["id"]])

    def send(self, **kwargs):
        def execute(**options):
            assert options["num_retries"] == 0
            self.sent.append(kwargs)
            if self.fail:
                raise TimeoutError("private provider failure")
            return {"id": "new"}

        return SimpleNamespace(execute=execute)

    def new_batch_http_request(self, callback):
        requests = []
        self.batches.append(requests)
        return SimpleNamespace(
            add=lambda request, request_id: requests.append((request_id, request)),
            execute=lambda: [callback(key, req.execute(), None) for key, req in requests],
        )

    def close(self):
        self.closed = True


def record(key="one", labels=None):
    return {
        "id": key,
        "labelIds": labels if labels is not None else ["SENT"],
        "internalDate": "1789380000000",
        "snippet": "private",
        "payload": {
            "headers": [{"name": "To", "value": "Recruiter <jobs@company.example>"}],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                    "body": {
                        "data": base64.urlsafe_b64encode("Hola ñ".encode()).decode(),
                        "size": 7,
                    },
                },
                {"mimeType": "application/pdf", "filename": "CV.pdf", "body": {"size": 123}},
            ],
        },
    }


def test_100_metadata_reads_use_two_batches_then_cache():
    service = Service(record(str(i)) for i in range(100))
    connector = native.NativeGmailConnector(service)
    assert connector.call("gmail_get_profile", {})["email"] == "sender@example.com"
    for _ in range(2):
        response = connector.call("gmail_search_emails", {"query": "in:sent"})
        assert len(response["emails"]) == 100
        assert response["emails"][0]["to"] == ["jobs@company.example"]
    assert [len(batch) for batch in service.batches] == [50, 50]
    assert connector.metrics == {
        "provider_round_trips": 5,
        "metadata_cache_hits": 100,
        "llm_calls": 0,
    }
    connector.close()
    assert service.closed


def test_drafts_never_cached_and_full_message_normalized():
    service = Service([record(labels=["DRAFT"])])
    connector = native.NativeGmailConnector(service)
    for _ in range(2):
        connector.call("gmail_search_emails", {"query": "in:drafts"})
    assert len(service.batches) == 2
    response = connector.call("gmail_read_email", {"message_id": "one"})
    assert response["payload"]["parts"][0]["body"]["content"] == "Hola ñ"
    assert response["payload"]["parts"][1]["body"]["size"] == 123


def test_native_send_mime_matches_authorized_mail_and_never_retries(mail):
    service = Service()
    connector = native.NativeGmailConnector(service)
    gateway = GmailBridge(connector)
    assert gateway.send(mail) == "new"
    raw = base64.urlsafe_b64decode(service.sent[0]["body"]["raw"])
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert parsed["To"] == mail.recipient and parsed["From"] == mail.account
    assert parsed["Subject"] == mail.subject
    assert parsed.get_body().get_content().rstrip("\n") == mail.body
    assert next(parsed.iter_attachments()).get_payload(decode=True) == b"%PDF-synthetic-only"
    service.fail = True
    with pytest.raises(DeliveryUnknown):
        gateway.send(mail)
    assert len(service.sent) == 2


def test_fail_closed_on_partial_metadata_response():
    service = Service([record()])
    service.new_batch_http_request = lambda callback: SimpleNamespace(
        add=lambda *a, **kw: None, execute=lambda: None
    )
    connector = native.NativeGmailConnector(service)
    with pytest.raises(DeliveryBlocked, match="incomplete_history"):
        connector.call("gmail_search_emails", {"query": "in:sent"})


def test_callback_failure_and_unsupported_operation():
    service = Service([record()])
    service.new_batch_http_request = lambda callback: SimpleNamespace(
        add=lambda *a, **kw: None, execute=lambda: callback("one", {}, ValueError())
    )
    connector = native.NativeGmailConnector(service)
    with pytest.raises(DeliveryBlocked):
        connector.call("gmail_search_emails", {"query": "in:sent"})
    with pytest.raises(ValueError):
        connector.call("unsupported", {})
    with pytest.raises(ValueError):
        connector.call("gmail_send_email", {"payload": {"parts": []}})


def test_optional_sdk_missing_is_actionable(monkeypatch):
    monkeypatch.setattr(native.importlib, "import_module", Mock(side_effect=ImportError))
    with pytest.raises(DeliveryBlocked, match="install_gmail_extra"):
        native._module("googleapiclient.discovery")


def test_service_uses_static_discovery_and_bounded_http(monkeypatch):
    modules = {
        name: Mock() for name in ("google_auth_httplib2", "httplib2", "googleapiclient.discovery")
    }
    monkeypatch.setattr(native, "_module", modules.__getitem__)
    native._service("credentials")
    modules["httplib2"].Http.assert_called_once_with(timeout=30)
    assert modules["googleapiclient.discovery"].build.call_args.kwargs["static_discovery"]


@pytest.mark.parametrize("valid", [True, False])
def test_connect_never_initiates_interactive_login(tmp_path, monkeypatch, valid):
    credential = SimpleNamespace(
        refresh_token="refresh" if valid else None, has_scopes=lambda s: True
    )
    sdk = SimpleNamespace(
        Credentials=SimpleNamespace(from_authorized_user_file=lambda *a: credential)
    )
    monkeypatch.setattr(native, "_module", lambda name: sdk)
    monkeypatch.setattr(native, "_service", lambda c: Service())
    if valid:
        native.connect(tmp_path / "token.json").close()
    else:
        with pytest.raises(DeliveryBlocked):
            native.connect(tmp_path / "token.json")


@pytest.mark.parametrize("case", ["success", "exists", "mismatch", "no_refresh", "no_scope"])
def test_auth_setup_guards_and_no_send(tmp_path, monkeypatch, case):
    credential = SimpleNamespace(
        refresh_token=None if case == "no_refresh" else "secret",
        has_scopes=lambda s: case != "no_scope",
        to_json=lambda: '{"secret":true}',
    )
    flow = SimpleNamespace(run_local_server=lambda **kw: credential)
    sdk = SimpleNamespace(
        InstalledAppFlow=SimpleNamespace(from_client_secrets_file=lambda *a: flow)
    )
    monkeypatch.setattr(native, "_module", lambda name: sdk)
    service = Service()
    monkeypatch.setattr(native, "_service", lambda c: service)
    token = tmp_path / "token.json"
    if case == "exists":
        token.write_text("preserve")
    if case == "success":
        native.authorize(tmp_path / "client.json", token, "sender@example.com")
        assert json.loads(token.read_text()) == {"secret": True}
    else:
        with pytest.raises(DeliveryBlocked):
            native.authorize(
                tmp_path / "client.json",
                token,
                "wrong@example.com" if case == "mismatch" else "sender@example.com",
            )
    assert not service.sent


def test_payload_absent_and_binary_data():
    assert native._payload({})["body"] == {"size": 0}
    assert (
        "content"
        not in native._payload({"mimeType": "application/pdf", "body": {"data": "YWJj"}})["body"]
    )


@pytest.mark.parametrize(
    "operation,reason",
    [
        ("auth", "gmail_authorization_failed"),
        ("connect", "gmail_credentials_unavailable"),
    ],
)
def test_sdk_setup_errors_are_redacted(tmp_path, monkeypatch, operation, reason):
    monkeypatch.setattr(native, "_module", Mock(side_effect=RuntimeError("secret provider data")))
    with pytest.raises(DeliveryBlocked, match=reason):
        if operation == "auth":
            native.authorize(tmp_path / "client.json", tmp_path / "token.json", "a@example.com")
        else:
            native.connect(tmp_path / "token.json")


def test_network_batch_failure_is_fail_closed():
    service = Service([record()])
    service.new_batch_http_request = lambda callback: SimpleNamespace(
        add=lambda *a, **kw: None, execute=Mock(side_effect=TimeoutError("private URL"))
    )
    with pytest.raises(DeliveryBlocked, match="incomplete_history"):
        native.NativeGmailConnector(service).call("gmail_search_emails", {"query": "in:sent"})
