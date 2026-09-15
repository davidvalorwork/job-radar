from pathlib import Path

import pytest
from pydantic import ValidationError

from job_radar.adapters.outbound.config import AppConfig, SourceCatalog, load_bundle, read_yaml
from job_radar.application.planning import SearchSource, plan_queries

ROOT = Path(__file__).resolve().parents[1]


def test_generic_catalog():
    config, catalog, fingerprint = load_bundle(ROOT / "config/example.yaml")
    assert len(catalog.sources) == 27
    assert len(fingerprint) == 64
    assert all(source.access_status == "unverified" for source in catalog.sources)
    assert config.profiles[0].id == "devops"
    assert load_bundle(ROOT / "config/example.yaml")[2] == fingerprint


def test_example_search_equation_covers_requested_roles_and_technologies():
    config, _, _ = load_bundle(ROOT / "config/example.yaml")
    profiles = {profile.id: profile for profile in config.profiles}
    assert set(profiles) >= {
        "devops",
        "fullstack_ai",
        "backend_engineering",
        "frontend_engineering",
        "fullstack_web",
        "serverless_lambda",
    }
    queries = " ".join(
        query.casefold() for profile in profiles.values() for query in profile.queries
    )
    for technology in ("node.js", "python", "django", "react", "angular", "lambda"):
        assert technology in queries
    assert [
        profiles[profile_id].priority
        for profile_id in ("devops", "fullstack_ai", "backend_engineering")
    ] == [100, 80, 75]


@pytest.mark.parametrize("field", ["network_enabled", "sending_enabled"])
def test_cannot_enable_external_side_effects(field):
    config, _, _ = load_bundle(ROOT / "config/example.yaml")
    data = config.model_dump(mode="json")
    data["runtime"][field] = True
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "blank", "string_bool"])
def test_strict_config(mutation):
    config, _, _ = load_bundle(ROOT / "config/example.yaml")
    data = config.model_dump(mode="json")
    if mutation == "unknown":
        data["typo"] = True
    elif mutation == "duplicate":
        data["profiles"].append(data["profiles"][0])
    elif mutation == "blank":
        data["profiles"][0]["queries"] = [" "]
    else:
        data["filters"]["remote_only"] = "false"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(
    "payload",
    ["a: 1\na: 2", "a: &alias [1]\nb: *alias", "x" * 1_048_577],
    ids=["duplicate-key", "alias", "oversized"],
)
def test_ambiguous_or_large_yaml_rejected(tmp_path, payload):
    path = tmp_path / "config.yaml"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        read_yaml(path)


@pytest.mark.parametrize("mutation", ["duplicate", "unsupported_enabled", "domain_without_url"])
def test_catalog_invariants(mutation):
    _, catalog, _ = load_bundle(ROOT / "config/example.yaml")
    data = catalog.model_dump(mode="json")
    if mutation == "duplicate":
        data["sources"].append(data["sources"][0])
    elif mutation == "unsupported_enabled":
        data["sources"][0]["query_support"] = "unavailable"
    else:
        data["sources"][0].update(query_support="domain", url=None)
    with pytest.raises(ValidationError):
        SourceCatalog.model_validate(data)


def test_query_plan_budget_scope_order_and_dedup():
    sources = [
        SearchSource("board", "job_board", 50, "example.com"),
        SearchSource("search", "web_search", 100),
    ]
    profiles = [("devops", ["devops", "DevOps", "sre"]), ("ai", ["fullstack ai"])]
    planned = plan_queries(sources, profiles, 3)
    assert len(planned) == 3
    assert planned[0].source_id == "search"
    assert planned[1].query == "site:example.com devops"
    assert planned[2].query == "sre"
    assert plan_queries([], profiles, 5) == ()
    assert len(plan_queries(sources, profiles, 100)) == 6


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_invalid_query_budget(limit):
    with pytest.raises(ValueError):
        plan_queries([], [], limit)
