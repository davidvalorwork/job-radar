"""Strict YAML boundary with size limits, duplicate-key and alias rejection."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from job_radar.domain.models import FilterPolicy, RoleProfile

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Category = Literal["web_search", "social", "job_board", "community", "company_page", "feed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileConfig(StrictModel):
    id: Identifier
    priority: int = Field(default=50, ge=0, le=100)
    title_terms: list[str] = Field(min_length=1, max_length=64)
    preferred_terms: list[str] = Field(default_factory=list, max_length=64)
    required_context_any: list[str] = Field(default_factory=list, max_length=64)
    queries: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def nonempty_terms(self) -> ProfileConfig:
        for values in (
            self.title_terms,
            self.preferred_terms,
            self.required_context_any,
            self.queries,
        ):
            if any(not value.strip() or len(value) > 512 for value in values):
                raise ValueError("Terms must be non-empty and at most 512 characters")
        return self

    def domain(self) -> RoleProfile:
        return RoleProfile(
            self.id,
            self.priority,
            tuple(self.title_terms),
            tuple(self.preferred_terms),
            tuple(self.required_context_any),
        )


class FiltersConfig(StrictModel):
    remote_only: bool = True
    max_age_days: int | None = Field(default=30, ge=1, le=3650)
    languages: list[str] = Field(default_factory=lambda: ["en", "es"], max_length=20)
    excluded_companies: list[str] = Field(default_factory=list, max_length=1000)
    min_monthly_salary_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    include_undisclosed_salary: bool = True

    def domain(self) -> FilterPolicy:
        return FilterPolicy(
            self.remote_only,
            self.max_age_days,
            tuple(self.languages),
            tuple(self.excluded_companies),
            self.min_monthly_salary_usd,
            self.include_undisclosed_salary,
        )


class RuntimeConfig(StrictModel):
    network_enabled: Literal[False] = False
    sending_enabled: Literal[False] = False
    batch_size: int = Field(default=100, ge=1, le=1000)
    max_records: int = Field(default=2000, ge=1, le=100000)
    max_queries: int = Field(default=80, ge=1, le=1000)


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    sources_file: str = "sources.yaml"
    profiles: list[ProfileConfig] = Field(min_length=1, max_length=20)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def unique_profiles(self) -> AppConfig:
        if len({profile.id for profile in self.profiles}) != len(self.profiles):
            raise ValueError("Profile IDs must be unique")
        return self


class SourceConfig(StrictModel):
    id: Identifier
    label: str = Field(min_length=1, max_length=128)
    category: Category
    url: HttpUrl | None = None
    enabled: bool = False
    priority: int = Field(default=50, ge=0, le=100)
    query_support: Literal["keyword", "domain", "target_only", "unavailable"]
    command_refs: list[str] = Field(default_factory=list, max_length=16)
    tags: list[str] = Field(default_factory=list, max_length=20)
    access_status: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def validate_availability(self) -> SourceConfig:
        if self.enabled and self.query_support in {"target_only", "unavailable"}:
            raise ValueError("Sources requiring unimplemented target readers cannot be enabled")
        if self.query_support == "domain" and self.url is None:
            raise ValueError("Domain-scoped planning requires a URL")
        return self


class SourceCatalog(StrictModel):
    schema_version: Literal[1] = 1
    sources: list[SourceConfig] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_sources(self) -> SourceCatalog:
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        return self


class UniqueLoader(yaml.SafeLoader):
    """SafeLoader that refuses ambiguous duplicate mappings."""


def _unique_mapping(loader: UniqueLoader, node: MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError("Duplicate YAML key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def read_yaml(path: Path) -> Any:
    with path.open("rb") as stream:
        content = stream.read(1_048_577)
    if len(content) > 1_048_576:
        raise ValueError("Configuration exceeds 1 MiB")
    if any(isinstance(event, AliasEvent) for event in yaml.parse(content)):
        raise ValueError("YAML aliases are not supported")
    return yaml.load(content, Loader=UniqueLoader)


def load_bundle(path: Path) -> tuple[AppConfig, SourceCatalog, str]:
    config = AppConfig.model_validate(read_yaml(path))
    catalog = SourceCatalog.model_validate(read_yaml(path.parent / config.sources_file))
    payload = json.dumps(
        [config.model_dump(mode="json"), catalog.model_dump(mode="json")],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return config, catalog, sha256(payload).hexdigest()
