"""Deterministic job matching. Qualification is never permission to contact."""

import re
from datetime import datetime, timedelta
from functools import lru_cache

from job_radar.domain.models import Decision, Evaluation, FilterPolicy, Job, RoleProfile


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", re.IGNORECASE)


def contains_term(text: str, term: str) -> bool:
    return bool(term.strip()) and _term_pattern(term).search(text) is not None


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
    if job.published_at is None:
        review.append("publication_date_unknown")
    elif job.published_at > now:
        review.append("publication_date_in_future")
    elif now - job.published_at > timedelta(days=policy.max_age_days):
        rejected.append("publication_too_old")
    if job.language is None:
        review.append("language_unknown")
    elif policy.languages and job.language.casefold() not in {
        language.casefold() for language in policy.languages
    }:
        rejected.append("language_not_selected")
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
