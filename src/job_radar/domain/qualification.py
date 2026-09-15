"""Deterministic job matching. Qualification is never permission to contact."""

import re
from datetime import datetime, timedelta
from functools import lru_cache

from job_radar.domain.evidence import salary_evidence_requires_review
from job_radar.domain.models import Decision, Evaluation, FilterPolicy, Job, RoleProfile


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", re.IGNORECASE)


def contains_term(text: str, term: str) -> bool:
    return bool(term.strip()) and _term_pattern(term).search(text) is not None


def _salary_reason(job: Job, policy: FilterPolicy) -> tuple[str | None, bool]:
    """Return an auditable reason and whether it rejects, without guessing conversions."""
    threshold = policy.min_monthly_salary_usd
    if threshold is None:
        return None, False
    if salary_evidence_requires_review(job):
        return "salary_evidence_requires_review", False
    lower, upper = job.salary_min_monthly_usd, job.salary_max_monthly_usd
    if lower is None and upper is None:
        if job.salary_disclosed:
            return "salary_conversion_required", False
        if not policy.include_undisclosed_salary:
            return "salary_undisclosed", True
        return None, False
    if upper is not None and upper <= threshold:
        return "salary_at_or_below_threshold", True
    if lower is not None and lower > threshold:
        return None, False
    return "salary_range_requires_confirmation", False


def evaluate(
    job: Job, profiles: tuple[RoleProfile, ...], policy: FilterPolicy, now: datetime
) -> Evaluation:
    if now.utcoffset() is None:
        raise ValueError("The clock must provide a timezone-aware time")
    rejected: list[str] = []
    review: list[str] = []
    if job.company.casefold() in {company.casefold() for company in policy.excluded_companies}:
        rejected.append("company_excluded")
    if policy.remote_only:
        if job.remote is False:
            rejected.append("not_remote")
        elif job.remote is None:
            review.append("remote_unknown")
    if policy.max_age_days is not None:
        if job.published_at is None:
            review.append("publication_date_unknown")
        elif job.published_at > now:
            review.append("publication_date_in_future")
        elif now - job.published_at > timedelta(days=policy.max_age_days):
            rejected.append("publication_too_old")
    if policy.languages:
        if job.language is None:
            review.append("language_unknown")
        elif job.language.casefold() not in {language.casefold() for language in policy.languages}:
            rejected.append("language_not_selected")
    salary_reason, salary_rejected = _salary_reason(job, policy)
    if salary_reason:
        (rejected if salary_rejected else review).append(salary_reason)
    context = f"{job.title}\n{job.description}"
    profile = next(
        (
            role
            for role in sorted(profiles, key=lambda role: (-role.priority, role.id))
            if any(contains_term(job.title, term) for term in role.title_terms)
            and (
                not role.required_context_any
                or any(contains_term(context, term) for term in role.required_context_any)
            )
        ),
        None,
    )
    if profile is None:
        rejected.append("no_role_match")
    score = (
        min(100, 50 + sum(10 for term in profile.preferred_terms if contains_term(context, term)))
        if profile
        else 0
    )
    decision = Decision.REJECTED if rejected else Decision.REVIEW if review else Decision.QUALIFIED
    return Evaluation(decision, profile.id if profile else None, score, tuple(rejected + review))
