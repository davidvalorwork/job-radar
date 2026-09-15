"""Strict local manifests and immutable PDF snapshots; no live side effects."""

import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from pydantic import AwareDatetime, Field, HttpUrl

from job_radar.adapters.outbound.config import StrictModel, load_bundle, read_yaml
from job_radar.adapters.outbound.jsonl import JobRecord
from job_radar.adapters.outbound.posting_evidence import inspect_posting
from job_radar.domain.evidence import unsupported_aggregator_contact
from job_radar.domain.mail import Mail, digest
from job_radar.domain.models import Decision, Job, canonical_url
from job_radar.domain.qualification import evaluate


class PrimaryEvidence(StrictModel):
    url: HttpUrl
    text: str = Field(min_length=1, max_length=32000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fetched_at: AwareDatetime


class Candidate(StrictModel):
    job: JobRecord
    recipient: str = Field(max_length=254)
    company_key: str = Field(max_length=253)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=16000)
    contact_source_url: HttpUrl
    verified_at: AwareDatetime
    official_application_contact: bool
    suppression_reviewed: bool
    primary_evidence: PrimaryEvidence | None = None
    application_requirements_reviewed: bool = False


class Manifest(StrictModel):
    campaign: str = Field(pattern=r"^[a-z0-9_-]{1,80}$")
    sender: str = Field(max_length=254)
    cv: str
    candidates: list[Candidate] = Field(min_length=1, max_length=100)
    suppressed_recipients: list[str] = Field(default_factory=list, max_length=1000)
    suppressed_companies: list[str] = Field(default_factory=list, max_length=1000)


def prepare_plan(path: Path, config_path: Path) -> tuple[str, list[Mail], dict[str, int], Manifest]:
    # JSON is valid YAML; Pydantic JSON validation supports ISO dates in strict mode.
    manifest = Manifest.model_validate_json(json.dumps(read_yaml(path), default=str))
    config, catalog, config_hash = load_bundle(config_path)
    cv_path = (path.parent / manifest.cv).resolve()
    with cv_path.open("rb") as stream:
        cv = stream.read(2_000_001)
    if not cv.startswith(b"%PDF-") or len(cv) > 2_000_000:
        raise ValueError("A PDF CV of at most 2 MB is required")
    cv_base64, cv_sha = base64.urlsafe_b64encode(cv).decode(), sha256(cv).hexdigest()
    profiles = tuple(profile.domain() for profile in config.profiles)
    sources = {source.id for source in catalog.sources}
    mails = []
    primary_fingerprints = []
    counts = {"ready": 0, "review": 0, "rejected": 0}
    now = datetime.now(UTC)
    for item in manifest.candidates:
        if item.job.source_id not in sources:
            raise ValueError("Unregistered source")
        job = Job(**item.job.model_dump())
        evaluation = evaluate(job, profiles, config.filters.domain(), now)
        if not item.official_application_contact or not item.suppression_reviewed:
            counts["review"] += 1
            continue
        posting = inspect_posting(job.url, job.description, item.recipient)
        if posting:
            proof = item.primary_evidence
            primary = inspect_posting(str(proof.url), proof.text, item.recipient) if proof else None
            if (
                proof is None
                or primary is None
                or str(proof.url) != posting["primary_url"]
                or sha256(proof.text.encode()).hexdigest() != proof.sha256
                or not timedelta(0) <= now - proof.fetched_at <= timedelta(hours=24)
                or not primary["contact_present"]
                or "source_incomplete" in primary["flags"]
                or (primary["flags"] and not item.application_requirements_reviewed)
            ):
                counts["review"] += 1
                continue
            if "posting_closed" in primary["flags"]:
                counts["rejected"] += 1
                continue
        if canonical_url(
            str(item.contact_source_url)
        ) == job.url and unsupported_aggregator_contact(job.description, item.recipient):
            counts["review"] += 1
            continue
        if evaluation.decision != Decision.QUALIFIED:
            counts[str(evaluation.decision)] += 1
            continue
        mails.append(
            Mail(
                manifest.sender,
                item.recipient,
                item.company_key,
                item.subject,
                item.body,
                job.url,
                str(item.primary_evidence.url)
                if posting and item.primary_evidence
                else str(item.contact_source_url),
                min(item.verified_at, item.primary_evidence.fetched_at).isoformat()
                if posting and item.primary_evidence
                else item.verified_at.isoformat(),
                "CV.pdf",
                cv_base64,
                len(cv),
                cv_sha,
            )
        )
        if posting and item.primary_evidence:
            primary_fingerprints.append(
                {
                    "mail": mails[-1].key,
                    "url": str(item.primary_evidence.url),
                    "sha256": item.primary_evidence.sha256,
                    "fetched_at": item.primary_evidence.fetched_at.isoformat(),
                    "requirements_reviewed": item.application_requirements_reviewed,
                }
            )
        counts["ready"] += 1
    batch = digest(
        {
            "campaign": manifest.campaign,
            "config_hash": config_hash,
            "mails": [asdict(mail) for mail in mails],
            "suppressions": [manifest.suppressed_recipients, manifest.suppressed_companies],
            **({"primary_evidence": primary_fingerprints} if primary_fingerprints else {}),
        }
    )
    return batch, mails, counts, manifest
