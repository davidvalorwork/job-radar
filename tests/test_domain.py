from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from job_radar.domain.contact_policy import may_contact
from job_radar.domain.models import Decision, FilterPolicy, canonical_url
from job_radar.domain.qualification import contains_term, evaluate


@pytest.mark.parametrize(
    "description",
    [
        "Salary: $120k-$180k per year",
        "USD 60000/year",
        "$4000/mo",
        "Equity-only until seed funding; cash compensation afterwards.",
        "No cash salary until funding.",
    ],
)
def test_undisclosed_flag_cannot_hide_explicit_compensation(job, profiles, now, description):
    policy = FilterPolicy(min_monthly_salary_usd=2000)
    result = evaluate(replace(job, description=description), profiles, policy, now)
    assert result.decision == Decision.REVIEW
    assert "salary_evidence_requires_review" in result.reasons


def test_conditional_equity_is_not_current_cash_salary(job, profiles, now):
    item = replace(
        job,
        description="Equity-only pre-seed, $200k post-seed",
        salary_disclosed=True,
        salary_min_monthly_usd=16000,
    )
    result = evaluate(item, profiles, FilterPolicy(min_monthly_salary_usd=2000), now)
    assert result.decision == Decision.REVIEW


@pytest.mark.parametrize(
    "description",
    [
        "Raised $5M in funding. Salary not disclosed.",
        "Paid salary plus equity.",
        "This is not equity-only.",
        "We have 120000 users.",
    ],
)
def test_funding_and_equity_bonus_are_not_salary_evidence(job, profiles, now, description):
    assert (
        evaluate(
            replace(job, description=description),
            profiles,
            FilterPolicy(min_monthly_salary_usd=2000),
            now,
        ).decision
        == Decision.QUALIFIED
    )


def test_canonical_identity(job):
    alternate = replace(
        job, source_id="indeed", url="https://EXAMPLE.com:443/jobs/1?utm_source=x#top"
    )
    assert alternate.key == job.key
    assert (
        canonical_url("https://example.com?z=2&id=42&utm_campaign=x")
        == "https://example.com/?id=42&z=2"
    )
    assert canonical_url("https://[::1]:443/") == "https://[::1]/"
    assert canonical_url("http://example.com:8080/a") == "http://example.com:8080/a"


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "https://user:pass@example.com",
        "/jobs/1",
        "https://example.com:99999",
        "https://example.com:0",
    ],
)
def test_invalid_identity(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_aware_dates_and_required_fields(job):
    with pytest.raises(ValueError):
        replace(job, published_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        replace(job, title=" ")


def test_priority_and_score(job, profiles, now):
    result = evaluate(replace(job, title="DevOps / AI Engineer"), profiles, FilterPolicy(), now)
    assert result.decision == Decision.QUALIFIED
    assert result.profile_id == "devops"
    assert result.score == 60


def test_ai_context_required(job, profiles, now):
    generic = replace(job, title="Full Stack Engineer")
    assert evaluate(generic, profiles, FilterPolicy(), now).decision == Decision.REJECTED
    with_ai = replace(generic, description="Python and LLM applications")
    assert evaluate(with_ai, profiles, FilterPolicy(), now).profile_id == "fullstack_ai"


@pytest.mark.parametrize(
    ("field", "value", "reason", "decision"),
    [
        ("remote", None, "remote_unknown", Decision.REVIEW),
        ("remote", False, "not_remote", Decision.REJECTED),
        ("published_at", None, "publication_date_unknown", Decision.REVIEW),
        ("language", None, "language_unknown", Decision.REVIEW),
        ("language", "de", "language_not_selected", Decision.REJECTED),
        ("title", "Designer", "no_role_match", Decision.REJECTED),
    ],
)
def test_unknown_and_negative_evidence(job, profiles, now, field, value, reason, decision):
    result = evaluate(replace(job, **{field: value}), profiles, FilterPolicy(), now)
    assert result.decision == decision
    assert reason in result.reasons


def test_age_and_exclusions(job, profiles, now):
    stale = replace(job, published_at=now - timedelta(days=31))
    future = replace(job, published_at=now + timedelta(minutes=1))
    assert "publication_too_old" in evaluate(stale, profiles, FilterPolicy(), now).reasons
    assert evaluate(future, profiles, FilterPolicy(), now).decision == Decision.REVIEW
    assert (
        "company_excluded"
        in evaluate(
            job, profiles, FilterPolicy(excluded_companies=("EXAMPLE COMPANY",)), now
        ).reasons
    )
    relaxed = FilterPolicy(remote_only=False, max_age_days=60, languages=())
    assert (
        evaluate(replace(stale, remote=False, language="fr"), profiles, relaxed, now).decision
        == Decision.QUALIFIED
    )
    with pytest.raises(ValueError):
        evaluate(job, profiles, FilterPolicy(), now.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("chair", "ai", False),
        ("AI Engineer", "ai", True),
        ("Terraform", " ", False),
        ("C++ Developer", "C++", True),
    ],
)
def test_term_boundaries(text, term, expected):
    assert contains_term(text, term) is expected


def contact_args(now):
    return dict(
        now=now,
        last_recipient_contact=None,
        last_company_contact=None,
        suppressed=False,
        unresolved_send=False,
        official_contact_verified=True,
    )


@pytest.mark.parametrize("field", ["last_recipient_contact", "last_company_contact"])
@pytest.mark.parametrize(
    ("hours", "expected"), [(47.99, False), (48, True), (480, True), (-1, False)]
)
def test_48_hour_boundary(now, field, hours, expected):
    args = contact_args(now)
    args[field] = now - timedelta(hours=hours)
    assert may_contact(**args) is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("suppressed", True), ("unresolved_send", True), ("official_contact_verified", False)],
)
def test_contact_safety_is_not_relaxed(now, field, value):
    args = contact_args(now)
    args[field] = value
    assert may_contact(**args) is False


def test_contact_clock_validation(now):
    args = contact_args(now)
    args["last_company_contact"] = now.replace(tzinfo=None)
    with pytest.raises(ValueError):
        may_contact(**args)
    args = contact_args(now)
    args["cooldown_hours"] = 0
    with pytest.raises(ValueError):
        may_contact(**args)
