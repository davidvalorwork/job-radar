import ast
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from job_radar.adapters.outbound.telemetry import JsonTelemetry
from job_radar.bootstrap import main

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ARGS = ["--config", str(ROOT / "config/example.yaml")]


def test_offline_vertical_slice(tmp_path, capsys, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("Offline commands must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    target = ["--data-dir", str(tmp_path / "state")]
    assert main(CONFIG_ARGS + ["config", "validate"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"] == 27
    assert main(CONFIG_ARGS + ["plan", "--limit", "5"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["count"] == 5 and plan["executed"] is False
    assert main(["init", *target]) == 0
    capsys.readouterr()
    for expected in ("inserted", "unchanged"):
        assert (
            main(CONFIG_ARGS + ["ingest", "--input", str(ROOT / "examples/jobs.jsonl"), *target])
            == 0
        )
        assert json.loads(capsys.readouterr().out)[expected] == 4
    assert main(["status", *target]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == 4
    assert main(["metrics", *target]) == 0
    metrics = capsys.readouterr().out
    assert "radar_cloud_llm_enabled 0.0" in metrics
    assert "example.com" not in metrics


def test_catalog_selection(capsys):
    assert main(CONFIG_ARGS + ["catalog", "--enabled-only", "--category", "social"]) == 0
    sources = json.loads(capsys.readouterr().out)
    assert sources and all(
        source["enabled"] and source["category"] == "social" for source in sources
    )
    assert main(CONFIG_ARGS + ["plan", "--source", "rss"]) == 0
    assert json.loads(capsys.readouterr().out)["queries"] == []
    assert main(CONFIG_ARGS + ["plan", "--source", "remoteok", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["queries"][0]["query"].startswith(
        "site:remoteok.com "
    )


@pytest.mark.parametrize(
    "args",
    [
        ["plan", "--limit", "0"],
        ["plan", "--limit", "201"],
        ["catalog", "--source", "not_registered"],
    ],
)
def test_cli_invalid_input(args, capsys):
    assert main(CONFIG_ARGS + args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_input"


def test_missing_config_safe_error(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "private-missing-file"), "config", "validate"]) == 3
    output = capsys.readouterr().out
    assert "io_error" in output
    assert "private-missing-file" not in output


def test_runtime_diagnostics(capsys):
    assert main(["doctor"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runtime_ok"] is True
    assert data["session_status"] == "not_checked"


def test_installed_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "job_radar.bootstrap", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    assert "ingest" in result.stdout


def test_diagnostic_output_failure_is_best_effort():
    class BrokenLogger:
        def info(self, *args, **kwargs):
            raise BrokenPipeError("closed diagnostic stream")

    telemetry = JsonTelemetry()
    telemetry.logger = BrokenLogger()
    telemetry.event("test", {"records": 1})


@pytest.mark.parametrize("layer", ["domain", "application"])
def test_hexagonal_import_boundaries(layer):
    allowed_internal = (
        ("job_radar.domain",)
        if layer == "domain"
        else (
            "job_radar.domain",
            "job_radar.application",
        )
    )
    for path in (ROOT / "src/job_radar" / layer).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "Keep core imports explicit for architectural checks"
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] in sys.stdlib_module_names or any(
                    module == allowed or module.startswith(allowed + ".")
                    for allowed in allowed_internal
                ), f"{path.name} imports forbidden dependency {module}"
