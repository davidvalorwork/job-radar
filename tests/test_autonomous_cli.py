import base64
import json
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_gmail_native import Service

from job_radar.adapters.inbound.cli import parser
from job_radar.adapters.outbound.gmail_native import NativeGmailConnector
from job_radar.adapters.outbound.mail_plan import prepare_plan
from job_radar.adapters.outbound.outbox import SQLiteOutbox
from job_radar.bootstrap import main


def test_mail_run_can_execute_without_an_assistant_bridge():
    args = parser().parse_args(
        ["mail", "run", "--batch", "hash", "--confirm", "hash", "--credentials", "token.json"]
    )
    assert args.credentials == Path("token.json")
    assert args.bridge_dir is None


def test_auth_cli_is_separate_and_does_not_enable_send(monkeypatch, capsys):
    authorize = Mock()
    monkeypatch.setattr("job_radar.bootstrap.authorize", authorize)
    assert (
        main(
            [
                "mail",
                "auth",
                "--client-secret",
                "client.json",
                "--credentials",
                "token.json",
                "--account",
                "sender@example.com",
            ]
        )
        == 0
    )
    authorize.assert_called_once_with(Path("client.json"), Path("token.json"), "sender@example.com")
    assert json.loads(capsys.readouterr().out) == {"authorized": True, "sending_enabled": False}


class RoundTripService(Service):
    """Offline provider double: reconstruct the full response from actual outgoing MIME."""

    def get(self, **kwargs):
        if self.fail:
            self.fail = False
            return SimpleNamespace(execute=Mock(side_effect=TimeoutError("private")))
        return super().get(**kwargs)

    def send(self, **kwargs):
        def execute(**options):
            assert options == {"num_retries": 0}
            self.sent.append(kwargs)
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(kwargs["body"]["raw"])
            )
            parts = []
            for part in message.iter_parts():
                data = part.get_payload(decode=True)
                parts.append(
                    {
                        "mimeType": part.get_content_type(),
                        "filename": part.get_filename() or "",
                        "headers": [{"name": k, "value": str(v)} for k, v in part.items()],
                        "body": {
                            "data": base64.urlsafe_b64encode(data).decode(),
                            "size": len(data),
                        },
                    }
                )
            self.records["new"] = {
                "id": "new",
                "labelIds": ["SENT"],
                "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
                "payload": {
                    "mimeType": message.get_content_type(),
                    "parts": parts,
                    "headers": [{"name": k, "value": str(v)} for k, v in message.items()],
                },
            }
            return {"id": "new"}

        return SimpleNamespace(execute=execute)


def test_direct_cli_send_verify_reconcile_and_restart_without_bridge(
    plan_files, tmp_path, monkeypatch, capsys
):
    path, config, _ = plan_files
    batch, mails, _, _ = prepare_plan(path, config)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.prepare(batch, mails, datetime.now(UTC))
    outbox.close()
    service = RoundTripService()
    service.fail = True
    connector = NativeGmailConnector(service)
    monkeypatch.setattr("job_radar.bootstrap.connect", lambda path: connector)
    monkeypatch.setattr("job_radar.bootstrap.FileConnector", Mock(side_effect=AssertionError))
    common = ["--batch", batch, "--data-dir", str(tmp_path), "--credentials", "token.json"]
    run = ["mail", "run", "--confirm", batch, *common]
    assert main(run) == 5
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {"sent": 0, "blocked": 0, "unknown": 1}
    assert lines[1]["transport"] == "gmail_oauth" and lines[1]["llm_calls"] == 0
    assert service.closed and len(service.sent) == 1
    assert main(["mail", "reconcile", "--mail-id", mails[0].key, *common]) == 0
    capsys.readouterr()
    assert main(run) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["sent"] == 0
    assert len(service.sent) == 1
