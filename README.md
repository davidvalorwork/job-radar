# Job Radar

Local-first job discovery with a modular, hexagonal architecture. Apache-2.0.

**Early release:** offline planning, opt-in cached web collection and authorized Gmail delivery.
No paid APIs or LLM calls are used by the implemented commands. Account sessions,
credentials, contact history, and personal configuration must stay outside Git.

**Documentación en español:** [guía e índice](docs/README.md),
[uso de la CLI](docs/usage.md), [configuración](docs/configuration.md),
[operación y errores](docs/operations.md), [desarrollo](docs/development.md),
[calidad y Git Flow](docs/quality.md), [ejecución sin asistente](docs/autonomous.md),
[reducción de contexto y tokens](docs/token-efficiency.md),
[Chrome local sin IA (experimental)](docs/chrome-worker.md),
[arquitectura poliglota](docs/polyglot-architecture.md).

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

- `domain/`: immutable jobs, deterministic qualification, technology/role profiles and future contact guards.
- `application/`: use cases and ports; no database, CLI, or third-party imports.
- `adapters/inbound/`: command-line interface.
- `adapters/outbound/`: YAML configuration, JSONL input, SQLite, clock, telemetry.
- `bootstrap.py`: composition root that wires concrete implementations.
- `config/`: generic preferences and a 27-source catalog. Selected != connected.

Implemented: strict config/catalog validation, bounded offline query planning for DevOps,
Full Stack AI, backend (Node.js/Python/Django), frontend (React/Angular), full stack web
and serverless (AWS Lambda),
streaming JSONL ingestion, rule-based matching, idempotent SQLite persistence,
transactional audit, resource diagnostics and local Prometheus-format snapshots.
`plan --output` feeds `collect --allow-network`: bounded parallel Agent Reach routes,
SQLite evidence cache, deadlines and provider circuit breakers. Raw pages stay local.

Planned: OpenCLI execution, authorized session checks, adaptive parallel discovery,
durable task leases/cursors, source-specific normalization, full filter inheritance,
and OTel/Grafana diagnostics.

**Mail:** `mail compose/prepare/run/status/reconcile/audit` implement factual templates, a durable outbox,
48-hour recipient/company cooldown, configurable rolling quota up to 500, persistent suppression
and provider read-back. Direct Gmail OAuth (`--credentials`) runs without an assistant
after one-time `mail auth`. Batched metadata reads and a SENT metadata cache reduce
provider requests; sending remains sequential and never blindly retries. The older
`--bridge-dir` transport still requires an assistant. See [email setup](docs/mail.md)
and [autonomy boundaries](docs/autonomous.md): arbitrary page extraction/contact validation
is not automatic, and Gmail still needs your own OAuth authorization.
Offline commands and configuration flags do not silently activate sending.
`mail audit` checks ledger coverage for explicit provider IDs without network or writes;
it does not prove delivery or application quality. See the
[execution audit and corrective actions](docs/execution-audit.md).
See [pre-campaign closeout](docs/campaign-closeout.md) for explicit external receipt
reconciliation, holds, primary-source evidence and assisted-versus-autonomous limits.

See [architecture](docs/architecture.md), [roadmap](docs/roadmap.md),
[contributing](CONTRIBUTING.md), and [security](SECURITY.md).

## Development

```sh
uv sync --locked
uv run pre-commit install
uv run python scripts/quality.py all
```

Use `scripts/quality.py format` to apply Ruff fixes/formatting, `check` for read-only
static checks, and `test` for tests plus coverage/JUnit reports. The application
coverage threshold is 95%. Hooks enforce work-branch names and Conventional Commit
subjects. CI runs static checks, Windows/Linux tests, PR policy and a final
`Quality gate`; remote branch rules must be activated separately. See the
[quality guide](docs/quality.md) before opening a pull request.

Configuration errors and failed imports return nonzero exit codes. Completed
batches remain committed if a later input record fails; the overall run is marked
failed. Re-running the input is safe for unchanged records. This is not yet a
durable background scheduler, and no exactly-once external delivery is claimed.
