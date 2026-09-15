import json
from copy import deepcopy

import pytest

from job_radar.adapters.outbound.mail_templates import compose
from job_radar.bootstrap import main


def files(plan_files, tmp_path):
    source, _, manifest = plan_files
    for field in ("body", "subject"):
        manifest["candidates"][0].pop(field)
    rows = []
    for index in range(100):
        candidate = deepcopy(manifest["candidates"][0])
        candidate["job"]["company"] = f"Company {index}"
        candidate["recipient"] = f"jobs@company{index}.example"
        candidate["company_key"] = f"company{index}.example"
        rows.append(candidate)
    manifest["candidates"] = rows
    source.write_text(json.dumps(manifest))
    profile = {
        "sender": manifest["sender"],
        "facts_reviewed": True,
        "signature": "Synthetic Person",
        "portfolio": "https://portfolio.example",
        "examples": [{"title_terms": ["devops"], "text": "User-reviewed synthetic experience."}],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    return source, path, manifest, profile


def test_compose_100_without_model_and_without_self_approving(plan_files, tmp_path, capsys):
    source, profile_path, manifest, _ = files(plan_files, tmp_path)
    manifest["candidates"][0]["official_application_contact"] = False
    source.write_text(json.dumps(manifest))
    target = tmp_path / "result.json"
    assert (
        main(
            [
                "mail",
                "compose",
                "--input",
                str(source),
                "--profile",
                str(profile_path),
                "--output",
                str(target),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"composed": 100, "unmatched": 0, "llm_calls": 0}
    rendered = json.loads(target.read_text())
    assert rendered["candidates"][0]["official_application_contact"] is False
    assert len({r["body"] for r in rendered["candidates"]}) == 100
    assert all("User-reviewed synthetic experience." in r["body"] for r in rendered["candidates"])
    with pytest.raises(ValueError):
        compose(source, profile_path, target)


@pytest.mark.parametrize("case", ["unreviewed", "account", "header", "placeholder"])
def test_profile_guards(plan_files, tmp_path, case):
    source, path, manifest, profile = files(plan_files, tmp_path)
    if case == "unreviewed":
        profile["facts_reviewed"] = False
    if case == "account":
        profile["sender"] = "other@example.com"
    if case == "header":
        profile["subject"] = "Bad\nHeader"
    if case == "placeholder":
        profile["body"] = "$unknown"
    path.write_text(json.dumps(profile))
    with pytest.raises((ValueError, KeyError)):
        compose(source, path, tmp_path / "result.json")


def test_unmatched_role_is_not_filled_with_invented_experience(plan_files, tmp_path):
    source, path, manifest, _ = files(plan_files, tmp_path)
    manifest["candidates"][0]["job"]["title"] = "Unrelated position"
    source.write_text(json.dumps(manifest))
    assert compose(source, path, tmp_path / "result.json")["unmatched"] == 1
