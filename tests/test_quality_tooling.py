import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import quality

ROOT = Path(__file__).resolve().parents[1]


def test_check_is_non_mutating_and_all_includes_test_and_build():
    checks = quality.commands("check")
    assert not any("--fix" in command for command in checks)
    assert any("--check" in command for command in checks)
    complete = quality.commands("all")
    assert complete == checks + quality.commands("test") + quality.commands("build")


def test_quality_stops_after_first_failure(monkeypatch):
    called = []

    def run(command, **kwargs):
        called.append(command)
        assert kwargs["cwd"] == ROOT and kwargs["timeout"] == 600
        assert not kwargs.get("shell", False)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(quality.subprocess, "run", run)
    assert quality.main(["all"]) == 7
    assert len(called) == 1


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("tool", 600)])
def test_quality_reports_tool_errors(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(quality.subprocess, "run", run)
    assert quality.main(["check"]) == 1


def test_quality_success(monkeypatch):
    monkeypatch.setattr(
        quality.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    assert quality.main(["format"]) == 0


def test_workflow_security_and_aggregate_gate():
    # BaseLoader preserves GitHub's literal `on` key instead of YAML 1.1 boolean coercion.
    config = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert "pull_request" in config["on"]
    assert "pull_request_target" not in config["on"]
    assert "edited" in config["on"]["pull_request"]["types"]
    assert config["permissions"] == {"contents": "read"}
    jobs = config["jobs"]
    assert any(
        step.get("run") == "uv run pre-commit run --all-files"
        and step.get("env") == {"SKIP": "branch-policy"}
        for step in jobs["static"]["steps"]
    )
    assert any(
        step.get("uses", "").startswith("actions/setup-python@")
        and step["with"].get("python-version-file") == ".python-version"
        for step in jobs["git-policy"]["steps"]
    )
    assert set(jobs["quality-gate"]["needs"]) == {"static", "tests", "git-policy"}
    assert "always()" in jobs["quality-gate"]["if"]
    for job in jobs.values():
        assert "timeout-minutes" in job
        assert "continue-on-error" not in job
        assert "permissions" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"


def test_quality_threshold_and_reports_are_centralized():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["coverage"]["report"]["fail_under"] == 95
    command = quality.commands("test")[0]
    assert "--cov" in command
    assert not any(arg.startswith("--cov-fail-under") for arg in command)
    assert "--junitxml=reports/junit.xml" in command


def test_dependabot_uses_uv_to_update_the_lockfile():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert {entry["package-ecosystem"] for entry in config["updates"]} == {"uv", "github-actions"}
    assert all(entry["target-branch"] == "develop" for entry in config["updates"])
