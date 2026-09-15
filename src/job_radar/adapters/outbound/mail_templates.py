"""Deterministic messages from user-reviewed facts; no generated qualifications."""

import json
from pathlib import Path
from string import Template

from pydantic import Field

from job_radar.adapters.outbound.config import StrictModel, read_yaml
from job_radar.adapters.outbound.mail_plan import Manifest
from job_radar.domain.qualification import contains_term


class Example(StrictModel):
    title_terms: list[str] = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=4000)


class MessageProfile(StrictModel):
    sender: str
    facts_reviewed: bool = False
    portfolio: str = Field(max_length=1000)
    signature: str = Field(min_length=1, max_length=1000)
    examples: list[Example] = Field(min_length=1, max_length=32)
    subject: str = "Application: $title - $signature"
    body: str = (
        "Hello $company hiring team,\n\n"
        "I am applying for the $title opening ($url).\n\n"
        "$example\n\nMy CV is attached. Project details: $portfolio\n\n$signature"
    )


def compose(input_path: Path, profile_path: Path, output_path: Path) -> dict[str, int]:
    if output_path.exists():
        raise ValueError("Refusing to overwrite a manifest")
    data = read_yaml(input_path)
    profile = MessageProfile.model_validate(read_yaml(profile_path))
    if not profile.facts_reviewed or data["sender"] != profile.sender:
        raise ValueError("Reviewed facts for the manifest account are required")
    unmatched = 0
    candidates = []
    for row in data["candidates"]:
        job = row["job"]
        example = next(
            (
                item
                for item in profile.examples
                if any(contains_term(job["title"], term) for term in item.title_terms)
            ),
            None,
        )
        if example is None:
            unmatched += 1
            continue
        fields = {
            "title": job["title"],
            "company": job["company"],
            "url": job["url"],
            "example": example.text,
            "portfolio": profile.portfolio,
            "signature": profile.signature,
        }
        # Strict substitution: unknown placeholders or control characters cannot silently leak.
        try:
            subject = Template(profile.subject).substitute(fields)
            body = Template(profile.body).substitute(fields)
        except KeyError as error:
            raise ValueError("Unsupported template placeholder") from error
        if any(char in subject for char in "\r\n\x00"):
            raise ValueError("Invalid subject")
        # Rendering replaces text that may have been reviewed against posting instructions.
        candidates.append(
            {**row, "subject": subject, "body": body, "application_requirements_reviewed": False}
        )
    data["candidates"] = candidates
    data["cv"] = str((input_path.parent / data["cv"]).resolve())
    manifest = Manifest.model_validate_json(json.dumps(data, default=str))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        output.write(manifest.model_dump_json(indent=2))
    return {"composed": len(candidates), "unmatched": unmatched, "llm_calls": 0}
