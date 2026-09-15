import pytest

from job_radar.adapters.outbound.job_normalizer import normalized_jobs, source_id
from job_radar.adapters.outbound.posting_evidence import inspect_posting
from job_radar.adapters.outbound.research import EvidenceCache, job_requests
from job_radar.application.collection import ResearchRequest, ResearchResponse


def test_generated_summary_cannot_supply_contact_or_role():
    text = (
        "Title: Designer\n| Apply via | listed@company.example |\n"
        "| Source | [HN](https://news.ycombinator.com/item?id=123) |\n"
        "## Original posting\nExample | DevOps Engineer | Remote\n"
        "Email [firstname]@company.example\n## Related\nlisted@company.example"
    )
    item = inspect_posting("https://hnhiring.fyi/job/1", text, "listed@company.example")
    assert item["title"] == "DevOps Engineer"
    assert item["primary_url"] == "https://news.ycombinator.com/item?id=123"
    assert "recipient_not_in_original" in item["flags"]
    assert item["requires_review"] is True


@pytest.mark.parametrize(
    "suffix,flag",
    [
        ("Do not use AI to write the initial message.", "application_instructions"),
        ("Use subject HN Engineer when applying.", "application_instructions"),
        ("US citizens only; security clearance required.", "geographic_eligibility"),
        ("This job is closed.", "posting_closed"),
    ],
)
def test_primary_source_retains_review_requirements(suffix, flag):
    text = (
        "Markdown Content:\nExample | DevOps Engineer | Remote\nApply jobs@company.example\n"
        + suffix
    )
    item = inspect_posting("https://news.ycombinator.com/item?id=123", text, "jobs@company.example")
    assert item["contact_present"] is True
    assert flag in item["flags"]
    assert item["source_digest"]


def test_incomplete_or_unsupported_source_never_approves():
    item = inspect_posting(
        "https://news.ycombinator.com/item?id=123",
        "Warning: not yet fully loaded\nMarkdown Content:\nExample | DevOps Engineer",
        "a@b.example",
    )
    assert "source_incomplete" in item["flags"]
    assert inspect_posting("https://not-hnhiring.fyi/job/1", "data", "a@b.example") is None


def test_normalization_keeps_primary_identity_and_expansion_skips_seen(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    cache = EvidenceCache(path)
    primary = "https://news.ycombinator.com/item?id=123"
    text = (
        "| Source | " + primary + " |\n## Original posting\n"
        "Example | DevOps Engineer | Remote\nApply jobs@company.example"
    )
    cache.put(ResearchRequest("web", "https://hnhiring.fyi/job/1"), ResearchResponse("ok", text))
    records = list(normalized_jobs(path, limit=10))
    assert records[0]["url"] == primary and records[0]["source_id"] == "hackernews"
    assert job_requests(path, limit=10, unseen_only=True) == [ResearchRequest("web", primary)]
    cache.put(ResearchRequest("web", primary), ResearchResponse("ok", "Markdown Content:\n" + text))
    cache.close()
    assert job_requests(path, limit=10, unseen_only=True) == []
    with pytest.raises(ValueError):
        job_requests(path, limit=10, unseen_only=True, after=1)
    assert source_id("https://notremoteok.com/jobs/1") == "company_pages"


def test_incomplete_source_is_not_normalized_into_a_qualified_job(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    cache = EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://hnhiring.fyi/job/1"),
        ResearchResponse("ok", "Title: DevOps Engineer\nSummary only"),
    )
    cache.close()
    assert list(normalized_jobs(path, limit=10)) == []
