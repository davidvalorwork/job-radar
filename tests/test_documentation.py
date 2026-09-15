"""Keep local documentation links and executable contracts aligned with the CLI."""

import re
import shlex
from pathlib import Path

from job_radar.adapters.inbound.cli import parser
from job_radar.adapters.outbound.config import load_bundle
from job_radar.adapters.outbound.jsonl import JobRecord
from job_radar.domain.models import Job

ROOT = Path(__file__).resolve().parents[1]
PAGES = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))


def test_documentation_local_links_resolve():
    for page in PAGES:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (page.parent / target.split("#", 1)[0]).resolve()
            assert resolved.is_relative_to(ROOT), f"Link leaves repository: {page.name}: {target}"
            assert resolved.exists(), f"Broken link: {page.name}: {target}"


def test_documented_cli_arguments_parse():
    checked = 0
    for page in PAGES:
        for block in re.findall(r"```sh\n(.*?)```", page.read_text(encoding="utf-8"), re.DOTALL):
            for command in block.splitlines():
                parts = shlex.split(command)
                if parts[:4] == ["uv", "run", "--extra", "gmail"]:
                    parts = parts[:2] + parts[4:]
                if parts[:3] != ["uv", "run", "radar"]:
                    continue
                if "--help" in parts:
                    continue
                parser().parse_args(parts[3:])
                checked += 1
    assert checked >= 20


def test_documented_job_example_matches_input_contract():
    text = (ROOT / "docs/usage.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    assert blocks
    _, catalog, _ = load_bundle(ROOT / "config/example.yaml")
    source_ids = {source.id for source in catalog.sources}
    for block in blocks:
        record = JobRecord.model_validate_json(block)
        job = Job(**record.model_dump())
        assert job.source_id in source_ids
        assert job.url.startswith("https://example.com/")
