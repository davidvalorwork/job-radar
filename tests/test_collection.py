import json
import sys
from types import SimpleNamespace

import pytest

from job_radar.adapters.outbound import research
from job_radar.adapters.outbound.job_normalizer import normalized_jobs
from job_radar.application.collection import CollectBatch, ResearchRequest, ResearchResponse
from job_radar.bootstrap import main


def test_plan_artifact_feeds_collector_without_assistant_transformation(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "plan.json"
    assert main(["plan", "--limit", "10", "--output", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "executed": False,
        "count": 10,
        "saved": True,
    }
    original = path.read_bytes()
    assert main(["plan", "--output", str(path)]) == 3
    capsys.readouterr()
    assert path.read_bytes() == original
    monkeypatch.setattr(
        research.AgentReachReader, "fetch", lambda self, req: ResearchResponse("ok", "result")
    )
    assert (
        main(
            [
                "collect",
                "--input",
                str(path),
                "--data-dir",
                str(tmp_path / "research"),
                "--allow-network",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["input"] == 10


@pytest.mark.parametrize(
    "query",
    [
        1,
        {"source_id": 1},
        {
            "source_id": "a",
            "profile_id": "b",
            "query": 3,
        },
    ],
)
def test_invalid_planned_query_rejected(tmp_path, query):
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "executed": False,
                "config_hash": "hash",
                "count": 1,
                "queries": [query],
            }
        )
    )
    with pytest.raises(ValueError):
        research.read_requests(path)


def test_100_duplicate_requests_fetch_once_then_zero_with_compact_cli(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def fetch(self, request):
        calls.append(request)
        return ResearchResponse("ok", "private raw page " * 10000)

    monkeypatch.setattr(research.AgentReachReader, "fetch", fetch)
    source = tmp_path / "requests.json"
    source.write_text(
        json.dumps([{"kind": "web", "value": "https://company.example/careers"}] * 100)
    )
    args = [
        "collect",
        "--allow-network",
        "--input",
        str(source),
        "--data-dir",
        str(tmp_path / "state"),
    ]
    for expected in (1, 0):
        assert main(args) == 0
        output = capsys.readouterr().out
        assert len(output) < 250 and "private" not in output
        assert json.loads(output)["fetched"] == expected
        assert json.loads(output)["llm_calls"] == 0
    assert len(calls) == 1


def test_cache_errors_expire_and_raw_audit_commits_together(tmp_path, monkeypatch):
    cache = research.EvidenceCache(tmp_path / "evidence.db")
    req = ResearchRequest("search", "DevOps")
    monkeypatch.setattr(research.time, "time", lambda: 1000)
    cache.put(req, ResearchResponse("reader_failed", ""))
    assert cache.fresh(req, 86400)
    monkeypatch.setattr(research.time, "time", lambda: 1301)
    assert not cache.fresh(req, 86400)
    assert cache.db.execute("SELECT count(*) FROM research_audit").fetchone()[0] == 1
    assert not cache.fresh(req, 0)
    with pytest.raises(ValueError):
        cache.put(req, ResearchResponse("ok", "x" * (research.LIMIT + 1)))
    cache.close()


def test_cached_success_survives_a_later_provider_failure(tmp_path, monkeypatch):
    cache = research.EvidenceCache(tmp_path / "evidence.db")
    request = ResearchRequest("search", "DevOps")
    monkeypatch.setattr(research.time, "time", lambda: 1000)
    cache.put(request, ResearchResponse("ok", "usable evidence"))
    monkeypatch.setattr(research.time, "time", lambda: 1001)
    cache.put(request, ResearchResponse("provider_limited", ""))
    assert cache.db.execute("SELECT status,content FROM evidence").fetchone() == (
        "ok",
        "usable evidence",
    )
    assert cache.db.execute("SELECT count(*) FROM research_audit").fetchone()[0] == 2
    cache.close()


def test_job_requests_only_follows_public_job_and_career_urls(tmp_path):
    path = tmp_path / "evidence.db"
    cache = research.EvidenceCache(path)
    cache.put(
        ResearchRequest("search", "DevOps"),
        ResearchResponse(
            "ok",
            " ".join(
                [
                    "https://jobs.example.com/roles/devops-engineer#apply",
                    "https://example.com/company/logo.png",
                    "https://careers.example.com/openings/platform-engineer",
                    "https://user:password@example.com/jobs/nope",
                ]
            ),
        ),
    )
    cache.close()
    assert research.job_requests(path, limit=10) == [
        ResearchRequest("web", "https://jobs.example.com/roles/devops-engineer"),
        ResearchRequest("web", "https://careers.example.com/openings/platform-engineer"),
    ]
    assert research.job_requests(path, limit=10, after=1) == [
        ResearchRequest("web", "https://careers.example.com/openings/platform-engineer")
    ]


def test_normalized_jobs_stays_local_and_does_not_create_contacts(tmp_path):
    path = tmp_path / "evidence.db"
    cache = research.EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://remoteok.com/remote-jobs/123-devops-at-example"),
        ResearchResponse(
            "ok",
            "Title: DevOps Engineer at Example\nMarkdown Content:\nRemote role. Salary: USD 5000",
        ),
    )
    cache.close()
    assert list(normalized_jobs(path, limit=1)) == [
        {
            "source_id": "remoteok",
            "external_id": "75489c0f27fd34aa46289ae224465ce6",
            "title": "DevOps Engineer at Example",
            "company": "Example",
            "url": "https://remoteok.com/remote-jobs/123-devops-at-example",
            "description": "\nRemote role. Salary: USD 5000",
            "remote": True,
            "salary_disclosed": True,
        }
    ]


def test_normalized_job_description_has_a_batch_safe_limit(tmp_path):
    path = tmp_path / "evidence.db"
    cache = research.EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://remoteok.com/remote-jobs/123-devops"),
        ResearchResponse("ok", "Title: DevOps Engineer\nMarkdown Content:\n" + "x" * 20_000),
    )
    cache.close()
    assert len(list(normalized_jobs(path, limit=1))[0]["description"]) == 8_000


def test_normalized_jobs_cursor_skips_prior_eligible_records(tmp_path):
    path = tmp_path / "evidence.db"
    cache = research.EvidenceCache(path)
    cache.put(
        ResearchRequest("web", "https://one.example/jobs"),
        ResearchResponse("ok", "# First role"),
    )
    cache.put(
        ResearchRequest("web", "https://two.example/jobs"),
        ResearchResponse("ok", "# Second role"),
    )
    cache.close()
    records = list(normalized_jobs(path, limit=1, after=1))
    assert records[0]["title"] == "Second role"


def test_collection_worker_error_is_recorded(tmp_path):
    cache = research.EvidenceCache(tmp_path / "evidence.db")
    reader = SimpleNamespace(fetch=lambda req: (_ for _ in ()).throw(OSError("secret")))
    batch = CollectBatch(reader, cache)
    assert batch.execute([ResearchRequest("search", "test")])["failed"] == 1
    for kwargs in ({"workers": 0}, {"workers": 9}, {"ttl": -1}, {"ttl": 604801}):
        with pytest.raises(ValueError):
            batch.execute([], **kwargs)
    with pytest.raises(ValueError):
        batch.execute([ResearchRequest("search", "test")] * 1001)
    cache.close()


@pytest.mark.parametrize(
    "kind,value",
    [
        ("shell", "whoami"),
        ("web", "http://example.com"),
        ("web", "https://localhost"),
        ("web", "https://127.0.0.1"),
        ("web", "https://a.local"),
        ("web", "https://user:pass@example.com"),
        ("web", "https://example.com:444"),
        ("search", ""),
        ("search", "a\nb"),
        ("search", "a" * 4097),
    ],
)
def test_research_request_validation(kind, value):
    with pytest.raises(ValueError):
        research.validate(ResearchRequest(kind, value))


@pytest.mark.parametrize(
    "rows",
    [
        {},
        [1],
        [{"kind": "web", "value": 1}],
        [{"kind": "web", "value": "https://example.com", "extra": True}],
        [{}] * 1001,
    ],
)
def test_strict_request_file(tmp_path, rows):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        research.read_requests(path)


def test_public_ip_and_search_allowed():
    research.validate(ResearchRequest("web", "https://1.1.1.1"))
    research.validate(ResearchRequest("search", 'DevOps "remote"'))


def test_reader_command_without_shell(tmp_path, monkeypatch):
    reader = research.AgentReachReader()
    monkeypatch.setattr(research.shutil, "which", lambda name: "/bin/" + name)
    assert "https://r.jina.ai/https://example.com" in reader._command(
        ResearchRequest("web", "https://example.com")
    )
    assert "query=x & whoami" in reader._command(ResearchRequest("search", "x & whoami"))
    wrapper = tmp_path / "mcporter.cmd"
    monkeypatch.setattr(
        research.shutil, "which", lambda name: str(wrapper) if name == "mcporter" else "node.exe"
    )
    with pytest.raises(ValueError):
        reader._command(ResearchRequest("search", "x"))
    entry = tmp_path / "node_modules/mcporter/dist/cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// synthetic")
    assert reader._command(ResearchRequest("search", "x"))[:2] == ["node.exe", str(entry)]
    monkeypatch.setattr(research.shutil, "which", lambda name: None)
    for kind in ("web", "search"):
        with pytest.raises(ValueError):
            reader._command(ResearchRequest(kind, "https://example.com"))


@pytest.mark.parametrize(
    "code,status",
    [
        ("print('public content')", "ok"),
        ("print('429 quota exceeded')", "provider_limited"),
        ("print('Title: DevOps vacancy\\n429 followers; experience with rate limits')", "ok"),
        ("print('Title: Just a moment...\\nPerforming security verification')", "reader_failed"),
        ("print('Warning: This page may require CAPTCHA')", "reader_failed"),
        ("print('Target URL returned error 404')", "reader_failed"),
        ("raise SystemExit(1)", "reader_failed"),
        ("print('x'*1100000)", "reader_budget_exceeded"),
    ],
)
def test_reader_real_subprocess_budget_and_circuit(monkeypatch, code, status):
    reader = research.AgentReachReader()
    monkeypatch.setattr(reader, "_command", lambda req: [sys.executable, "-c", code])
    request = ResearchRequest("search", "query")
    assert reader.fetch(request).status == status
    if status == "provider_limited":
        assert reader.fetch(request).status == "provider_circuit_open"


def test_worker_timeout_kills_process(monkeypatch):
    reader = research.AgentReachReader()
    monkeypatch.setattr(
        reader, "_command", lambda req: [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    ticks = iter([0, 46])
    monkeypatch.setattr(research.time, "monotonic", lambda: next(ticks))
    assert reader.fetch(ResearchRequest("search", "q")).status == "reader_budget_exceeded"
