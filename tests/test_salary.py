from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from job_radar.adapters.outbound.config import FiltersConfig
from job_radar.adapters.outbound.jsonl import JsonlJobReader
from job_radar.domain.models import Decision, FilterPolicy
from job_radar.domain.qualification import evaluate


@pytest.mark.parametrize(
    ("lower", "upper", "disclosed", "decision", "reason"),
    [
        (None, None, False, Decision.QUALIFIED, None),
        (None, None, True, Decision.REVIEW, "salary_conversion_required"),
        (2001, 3000, True, Decision.QUALIFIED, None),
        (2000.01, 2000.01, True, Decision.QUALIFIED, None),
        (2500, None, False, Decision.QUALIFIED, None),
        (2000, 2000, True, Decision.REJECTED, "salary_at_or_below_threshold"),
        (0, 1500, True, Decision.REJECTED, "salary_at_or_below_threshold"),
        (None, 2000, True, Decision.REJECTED, "salary_at_or_below_threshold"),
        (1800, 2500, True, Decision.REVIEW, "salary_range_requires_confirmation"),
        (2000, None, True, Decision.REVIEW, "salary_range_requires_confirmation"),
        (None, 4000, True, Decision.REVIEW, "salary_range_requires_confirmation"),
    ],
)
def test_salary_boundary(job, profiles, now, lower, upper, disclosed, decision, reason):
    offer = replace(
        job,
        salary_min_monthly_usd=lower,
        salary_max_monthly_usd=upper,
        salary_disclosed=disclosed,
    )
    result = evaluate(offer, profiles, FilterPolicy(min_monthly_salary_usd=2000), now)
    assert result.decision == decision
    assert result.reasons == (() if reason is None else (reason,))


def test_undisclosed_is_configurable_and_salary_is_optional(job, profiles, now):
    policy = FilterPolicy(min_monthly_salary_usd=2000, include_undisclosed_salary=False)
    result = evaluate(job, profiles, policy, now)
    assert result.decision == Decision.REJECTED
    assert result.reasons == ("salary_undisclosed",)
    assert evaluate(job, profiles, replace(policy, min_monthly_salary_usd=None), now).decision == (
        Decision.QUALIFIED
    )


@pytest.mark.parametrize("published", [None, -500, 3])
@pytest.mark.parametrize("language", [None, "de"])
def test_disabled_preferences_do_not_block_unknowns(job, profiles, now, published, language):
    offer = replace(
        job,
        remote=None,
        language=language,
        published_at=None if published is None else now + timedelta(days=published),
    )
    policy = FilterPolicy(
        remote_only=False, languages=(), max_age_days=None, min_monthly_salary_usd=2000
    )
    assert evaluate(offer, profiles, policy, now).decision == Decision.QUALIFIED


@pytest.mark.parametrize("amount", [-1, float("inf"), float("nan")])
def test_salary_domain_rejects_invalid_amounts(job, amount):
    for field in ("salary_min_monthly_usd", "salary_max_monthly_usd"):
        with pytest.raises(ValueError):
            replace(job, **{field: amount})
    with pytest.raises(ValueError):
        FilterPolicy(min_monthly_salary_usd=amount)


def test_salary_domain_rejects_inverted_range(job):
    with pytest.raises(ValueError):
        replace(job, salary_min_monthly_usd=3000, salary_max_monthly_usd=2500)


def test_private_preferences_schema_roundtrip():
    config = FiltersConfig.model_validate(
        dict(
            remote_only=False,
            max_age_days=None,
            languages=[],
            min_monthly_salary_usd=2000,
            include_undisclosed_salary=True,
        )
    )
    assert config.domain() == FilterPolicy(
        remote_only=False, max_age_days=None, languages=(), min_monthly_salary_usd=2000
    )


@pytest.mark.parametrize("amount", [-1, float("inf"), float("nan"), "2000", True])
def test_config_rejects_invalid_threshold(amount):
    with pytest.raises(ValidationError):
        FiltersConfig(min_monthly_salary_usd=amount)


@pytest.mark.parametrize("salary", ['"salary_min_monthly_usd":-1', '"salary_disclosed":"yes"'])
def test_jsonl_rejects_invalid_salary(tmp_path, salary):
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        '{"source_id":"example","external_id":"1","title":"DevOps",'
        '"company":"Example","url":"https://example.com/1",' + salary + "}\n"
    )
    with pytest.raises(ValueError, match="Invalid job record at line 1"):
        list(JsonlJobReader(path, {"example"}).read())


def test_jsonl_preserves_salary(tmp_path):
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        '{"source_id":"example","external_id":"1","title":"DevOps",'
        '"company":"Example","url":"https://example.com/1",'
        '"salary_min_monthly_usd":2500,"salary_max_monthly_usd":4000,"salary_disclosed":true}\n'
    )
    offer = list(JsonlJobReader(path, {"example"}).read())[0]
    assert offer.salary_min_monthly_usd == 2500
    assert offer.salary_max_monthly_usd == 4000
    assert offer.salary_disclosed is True
