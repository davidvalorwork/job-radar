from datetime import UTC, datetime

import pytest

from job_radar.domain.models import Job, RoleProfile


@pytest.fixture
def now():
    return datetime(2026, 9, 14, 12, tzinfo=UTC)


@pytest.fixture
def profiles():
    return (
        RoleProfile("devops", 100, ("devops", "sre", "platform engineer"), ("terraform",)),
        RoleProfile(
            "fullstack_ai",
            80,
            ("full stack", "full-stack", "ai engineer"),
            ("python",),
            ("ai", "llm", "rag"),
        ),
    )


@pytest.fixture
def job(now):
    return Job(
        "remoteok",
        "fixture-1",
        "DevOps Engineer",
        "Example Company",
        "https://example.com/jobs/1",
        "Terraform and Linux",
        True,
        now,
        "en",
    )
