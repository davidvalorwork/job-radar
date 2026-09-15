import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from job_radar.adapters.outbound.gmail_bridge import FileConnector, GmailBridge
from job_radar.domain.mail import DeliveryBlocked, DeliveryUnknown


class Connector:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, method, arguments):
        self.calls.append((method, arguments))
        return next(self.responses)


def verified_message(mail, **changes):
    result = {
        "id": "id1",
        "label_ids": ["SENT"],
        "internal_date": str(int(datetime.now(UTC).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": name, "value": value}
                for name, value in [
                    ("To", mail.recipient),
                    ("From", mail.account),
                    ("Subject", mail.subject),
                ]
            ],
            "parts": [
                {"mime_type": "text/plain", "body": {"content": mail.body}},
                {
                    "mime_type": "application/pdf",
                    "filename": mail.attachment_name,
                    "body": {"size": mail.attachment_size},
                },
            ],
        },
    }
    result.update(changes)
    return result


def test_gmail_send_profile_and_verify(mail):
    connector = Connector([{"email": mail.account}, {"id": "id1"}, verified_message(mail)])
    gateway = GmailBridge(connector)
    assert gateway.profile() == mail.account
    assert gateway.send(mail) == "id1"
    assert gateway.verify(mail, "id1").id == "id1"
    arguments = connector.calls[1][1]
    assert arguments["to"] == mail.recipient
    assert "bcc" not in arguments and "cc" not in arguments
    assert arguments["payload"]["parts"][1]["body"]["base64_url_content"] == mail.attachment_base64


@pytest.mark.parametrize("response", [{}, {"id": None}, {"id": ""}, {"id": 2}])
def test_missing_send_id_is_unknown(mail, response):
    with pytest.raises(DeliveryUnknown):
        GmailBridge(Connector([response])).send(mail)


@pytest.mark.parametrize(
    "case",
    [
        "id",
        "labels",
        "recipient",
        "sender",
        "subject",
        "cc",
        "bcc",
        "body",
        "no_body",
        "filename",
        "mime",
        "size",
        "extra",
        "future",
    ],
)
def test_verification_mismatch(mail, case):
    result = verified_message(mail)
    if case == "id":
        result["id"] = "different"
    elif case == "labels":
        result["label_ids"] = ["DRAFT"]
    elif case in {"recipient", "sender", "subject"}:
        index = {"recipient": 0, "sender": 1, "subject": 2}[case]
        result["payload"]["headers"][index]["value"] = "wrong"
    elif case in {"cc", "bcc"}:
        result["payload"]["headers"].append({"name": case, "value": "extra@example.com"})
    elif case == "body":
        result["payload"]["parts"][0]["body"]["content"] = "wrong"
    elif case == "no_body":
        result["payload"]["parts"].pop(0)
    elif case == "filename":
        result["payload"]["parts"][1]["filename"] = "different.pdf"
    elif case == "mime":
        result["payload"]["parts"][1]["mime_type"] = "text/plain"
    elif case == "size":
        result["payload"]["parts"][1]["body"]["size"] += 1
    elif case == "extra":
        result["payload"]["parts"].append(result["payload"]["parts"][1])
    else:
        result["internal_date"] = str(
            int((datetime.now(UTC) + timedelta(days=1)).timestamp() * 1000)
        )
    with pytest.raises(DeliveryUnknown):
        GmailBridge(Connector([result])).verify(mail, "id1")


def test_paginated_history_and_duplicates():
    item = {
        "id": "one",
        "labels": ["SENT"],
        "to": ["r@example.com"],
        "email_ts": "2026-09-14T12:00:00+00:00",
    }
    connector = Connector([{"emails": [item], "next_page_token": "page2"}, {"emails": [item]}])
    history = GmailBridge(connector).recent_sent(datetime.now(UTC) - timedelta(days=2))
    assert len(history) == 1
    assert history[0].recipients == ("r@example.com",)
    assert connector.calls[1][1]["next_page_token"] == "page2"


@pytest.mark.parametrize(
    "item",
    [
        {"id": "a", "labels": ["SENT"]},
        {"id": "a", "labels": ["INBOX"], "email_ts": "2026-09-14T00:00:00Z"},
        {"id": "a", "labels": ["SENT"], "email_ts": "2026-09-14T00:00:00Z", "to": []},
    ],
)
def test_invalid_history(item):
    with pytest.raises(DeliveryBlocked):
        GmailBridge(Connector([{"emails": [item]}])).recent_sent(datetime.now(UTC))


@pytest.mark.parametrize("repeat", [True, False])
def test_incomplete_pagination_blocks(repeat):
    pages = [{"emails": [], "next_page_token": "same" if repeat else str(i)} for i in range(20)]
    with pytest.raises(DeliveryBlocked, match="incomplete_history"):
        GmailBridge(Connector(pages)).search("in:sent")


@pytest.mark.parametrize(
    "label,text,expected",
    [
        ("DRAFT", "", "existing_draft_needs_review"),
        ("SENT", "old application", None),
        ("INBOX", "Thanks for your application", None),
        ("INBOX", "Do not contact us again", "provider_suppression"),
        ("INBOX", "Address not found, 5.1.1", "provider_suppression"),
    ],
)
def test_contact_safety(mail, label, text, expected):
    responses = [
        {"emails": [{"id": "x", "labels": [label]}]},
        {"payload": {"body": {"content": text}}},
    ]
    assert GmailBridge(Connector(responses)).contact_block(mail) == expected


def test_discarded_draft_does_not_block_but_trashed_optout_still_does(mail):
    for text, expected in [("Thanks", None), ("Do not contact us", "provider_suppression")]:
        connector = Connector(
            [
                {
                    "emails": [
                        {"id": "discarded", "labels": ["DRAFT", "TRASH"]},
                        {"id": "reply", "labels": ["TRASH"]},
                    ]
                },
                {"payload": {"body": {"content": text}}},
            ]
        )
        assert GmailBridge(connector).contact_block(mail) == expected
        assert connector.calls[1][1]["message_id"] == "reply"


@pytest.mark.parametrize("case", ["valid", "wrong_id", "error", "oversized", "malformed", "array"])
def test_file_bridge(tmp_path, case):
    connector = FileConnector(tmp_path, timeout=5)

    def respond():
        deadline = time.monotonic() + 4
        while not list(connector.directory.glob("*.request.json")):
            if time.monotonic() >= deadline:
                raise AssertionError("No request")
            time.sleep(0.01)
        request_path = next(connector.directory.glob("*.request.json"))
        request = json.loads(request_path.read_text())
        assert request["method"] == "gmail_get_profile"
        assert request["expires_at"] > time.time()
        response = {"id": request["id"], "result": {"email": "sender@example.com"}}
        if case == "wrong_id":
            response["id"] = "wrong"
        if case == "error":
            response["error"] = "provider unavailable"
        value = json.dumps(response)
        if case == "oversized":
            value = " " * 4_194_305
        if case == "malformed":
            value = "{"
        if case == "array":
            value = "[]"
        pending = request_path.with_suffix(".tmp")
        pending.write_text(value, encoding="utf-8")
        pending.replace(
            request_path.with_name(request_path.name.replace(".request.", ".response."))
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(respond)
        if case == "valid":
            assert connector.call("gmail_get_profile", {})["email"] == "sender@example.com"
        else:
            with pytest.raises((DeliveryUnknown, ValueError)):
                connector.call("gmail_get_profile", {})
        worker.result()


def test_bridge_timeout_and_allowlist(tmp_path):
    for timeout in [0, 301]:
        with pytest.raises(ValueError):
            FileConnector(tmp_path, timeout=timeout)
    connector = FileConnector(tmp_path, timeout=0.001)
    with pytest.raises(ValueError):
        connector.call("delete_everything", {})
    with pytest.raises(DeliveryUnknown, match="timeout"):
        connector.call("gmail_get_profile", {})
