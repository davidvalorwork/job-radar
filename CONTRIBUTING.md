# Contributing

Start with [AGENTS.md](AGENTS.md) and [architecture](docs/architecture.md).
Use Python 3.14 with SQLite >= 3.51.3, then `uv sync --locked`.

1. Follow [Git Flow and quality gates](docs/quality.md): create a focused
   `codex/<type>/<slug>` branch from `develop`, then propose a pull request.
2. Keep domain rules pure; inject external capabilities through application ports.
3. Add synthetic, offline tests for success, failure, and boundary conditions.
4. Run `uv run python scripts/quality.py all` (static checks, tests >= 95% application
   coverage, and build). Install local hooks with `uv run pre-commit install`.
5. Inspect the staged diff for private information before opening a pull request.

Do not submit real job contacts, inbox exports, session artifacts, or recordings.
Tests must never require account access. A catalog entry is not proof that a site
is supported: capability and authorization checks belong in a future adapter.

Changes to schemas or delivery policies need an explicit migration/recovery plan.
CI is configured for Windows and Linux using pinned action revisions. Require the
`Quality gate` check in remote rulesets before treating CI as a merge barrier; merely
committing a workflow does not protect a branch. Keep Conventional Commit PR titles,
including when using squash merges. The lockfile is committed; update dependencies
intentionally and rerun the full checks.

Contributions are licensed under Apache-2.0. Keep third-party notices and licensing
requirements when incorporating external code. This scaffold links to tools rather
than vendoring their implementations.
