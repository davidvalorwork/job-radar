import json

import pytest

from scripts import git_policy


@pytest.mark.parametrize(
    "branch",
    [
        "codex/feature/web-reader",
        "codex/fix/sqlite-lock",
        "codex/chore/quality-gates",
        "codex/release/0.2.0",
        "codex/hotfix/missing-date",
        "codex/docs/configuration",
    ],
)
def test_valid_work_branches(branch):
    assert git_policy.valid_work_branch(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "develop",
        "",
        "feature/x",
        "codex/random/x",
        "codex/fix/",
        "codex/fix/Bad Name",
        "codex/fix/x\ninjected",
    ],
)
def test_invalid_work_branches(branch):
    assert not git_policy.valid_work_branch(branch)


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add discovery",
        "fix(cli): handle missing input",
        "feat(api)!: change schema",
        "ci(deps): update actions",
        "docs: document configuración",
    ],
)
def test_conventional_subjects(subject):
    assert git_policy.valid_subject(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "update stuff",
        "Feat: add x",
        "feat:",
        "fix:  spaces",
        "fix: trailing ",
        "fix: a\nb",
        "feat: " + "x" * 95,
    ],
)
def test_invalid_subjects(subject):
    assert not git_policy.valid_subject(subject)


@pytest.mark.parametrize(
    ("head", "base", "expected"),
    [
        ("codex/feature/reader", "develop", True),
        ("codex/fix/x", "main", False),
        ("develop", "main", True),
        ("main", "develop", True),
        ("codex/release/0.2.0", "main", True),
        ("codex/hotfix/x", "main", True),
        ("codex/hotfix/x", "develop", True),
        ("codex/release/0.2.0", "develop", True),
        ("dependabot/pip/develop/ruff-1.0", "develop", True),
        ("dependabot/github_actions/actions/checkout-6", "main", False),
        ("codex/feature/x", "unknown", False),
        ("develop", "develop", False),
    ],
)
def test_pr_routes(head, base, expected):
    assert git_policy.valid_pr_route(head, base) is expected


def test_commit_hook_reads_subject_not_body(tmp_path):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("feat(cli): add command\n\nLong explanatory body.\n", encoding="utf-8")
    assert git_policy.main(["commit", str(path)]) == 0
    path.write_text("", encoding="utf-8")
    assert git_policy.main(["commit", str(path)]) == 1
    assert git_policy.main(["commit", str(tmp_path / "missing")]) == 2


def test_branch_hook(monkeypatch):
    monkeypatch.setattr(
        git_policy.subprocess, "check_output", lambda *a, **kw: "codex/test/hooks\n"
    )
    assert git_policy.main(["branch"]) == 0
    assert git_policy.main(["branch", "--name", "main"]) == 1


def test_pr_event_is_data_not_code(tmp_path, capsys):
    path = tmp_path / "event.json"
    event = {
        "pull_request": {
            "title": "ci: add quality checks",
            "head": {"ref": "codex/ci/checks"},
            "base": {"ref": "develop"},
        }
    }
    path.write_text(json.dumps(event), encoding="utf-8")
    assert git_policy.main(["pr", "--event", str(path)]) == 0
    event["pull_request"]["title"] = "$(echo secret-do-not-echo)"
    path.write_text(json.dumps(event), encoding="utf-8")
    assert git_policy.main(["pr", "--event", str(path)]) == 1
    assert "secret-do-not-echo" not in capsys.readouterr().out
    path.write_text('{"pull_request": null}', encoding="utf-8")
    assert git_policy.main(["pr", "--event", str(path)]) == 2
