# Job Radar

Local-first job discovery with a modular, hexagonal architecture. Apache-2.0.

**Bootstrap release:** functional offline workflows, not a live scraper or mail sender.
No paid APIs or LLM calls are used by the implemented commands. Account sessions,
credentials, contact history, and personal configuration must stay outside Git.

## Quick start

Requires Python 3.14, uv, and SQLite >= 3.51.3 in the selected Python interpreter.
The tested Python patch is recorded in `.python-version`.

```sh
git clone https://github.com/davidvalorwork/job-radar.git
cd job-radar
uv sync --locked
uv run radar doctor
uv run radar config validate
uv run radar catalog --enabled-only
uv run radar plan --limit 20
uv run radar init --data-dir .local/demo
uv run radar ingest --input examples/jobs.jsonl --data-dir .local/demo
uv run radar status --data-dir .local/demo
uv run radar metrics --data-dir .local/demo
```

All job examples are synthetic and use example.com. Repeating ingestion does not
duplicate unchanged jobs. Dates in fixtures are static, so matching results can
change as they age. Each run and per-job decision is audited locally.

## Architecture

- `domain/`: immutable jobs, deterministic qualification, future contact guards.
- `application/`: use cases and ports; no database, CLI, or third-party imports.
- `adapters/inbound/`: command-line interface.
- `adapters/outbound/`: YAML configuration, JSONL input, SQLite, clock, telemetry.
- `bootstrap.py`: composition root that wires concrete implementations.
- `config/`: generic preferences and a 27-source catalog. Selected != connected.

Implemented: strict config/catalog validation, bounded offline query planning,
streaming JSONL ingestion, rule-based matching, idempotent SQLite persistence,
transactional audit, resource diagnostics and local Prometheus-format snapshots.

Planned: OpenCLI execution, authorized session checks, adaptive parallel discovery,
durable task leases/cursors, source-specific normalization, full filter inheritance,
OTel/Grafana diagnostics, and independently authorized contact delivery.
**Changing configuration cannot enable network access or sending in this release.**

See [architecture](docs/architecture.md), [roadmap](docs/roadmap.md),
[contributing](CONTRIBUTING.md), and [security](SECURITY.md).

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
uv build
```

Configuration errors and failed imports return nonzero exit codes. Completed
batches remain committed if a later input record fails; the overall run is marked
failed. Re-running the input is safe for unchanged records. This is not yet a
durable background scheduler, and no exactly-once external delivery is claimed.
