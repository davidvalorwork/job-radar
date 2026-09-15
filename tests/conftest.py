import base64
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from job_radar.adapters.outbound.config import read_yaml
from job_radar.domain.mail import Mail
from job_radar.domain.models import Job, RoleProfile


@pytest.fixture
def plan_files(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = read_yaml(root / "config/example.yaml")
    config["sources_file"] = str(root / "config/sources.yaml")
    config["filters"].update(
        remote_only=False, max_age_days=None, languages=[], min_monthly_salary_usd=2000
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "cv.pdf").write_bytes(b"%PDF-test-only")
    manifest = {
        "campaign": "synthetic-test",
        "sender": "sender@example.com",
        "cv": "cv.pdf",
        "candidates": [
            {
                "job": {
                    "source_id": "remoteok",
                    "external_id": "one",
                    "title": "DevOps Engineer",
                    "company": "Synthetic Company",
                    "url": "https://company.example/job",
                },
                "recipient": "jobs@company.example",
                "company_key": "company.example",
                "subject": "DevOps application",
                "body": "Synthetic application body",
                "contact_source_url": "https://company.example/careers",
                "verified_at": datetime.now(UTC).isoformat(),
                "official_application_contact": True,
                "suppression_reviewed": True,
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, config_path, manifest


@pytest.fixture
def mail():
    cv = b"%PDF-synthetic-only"
    return Mail(
        "sender@example.com",
        "jobs@company.example",
        "company.example",
        "DevOps application",
        "Synthetic application body",
        "https://company.example/job",
        "https://company.example/careers",
        datetime.now(UTC).isoformat(),
        "CV.pdf",
        base64.urlsafe_b64encode(cv).decode(),
        len(cv),
        sha256(cv).hexdigest(),
    )


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
