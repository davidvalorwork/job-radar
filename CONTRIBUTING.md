# Contributing

Start with [AGENTS.md](AGENTS.md) and [architecture](docs/architecture.md).
Use Python 3.14 with SQLite >= 3.51.3, then `uv sync --locked`.

1. Create a focused branch and describe the behavior being changed.
2. Keep domain rules pure; inject external capabilities through application ports.
3. Add synthetic, offline tests for success, failure, and boundary conditions.
4. Run Ruff lint/format, mypy, pytest with coverage, and `uv build`.
5. Inspect the staged diff for private information before opening a pull request.

Do not submit real job contacts, inbox exports, session artifacts, or recordings.
Tests must never require account access. A catalog entry is not proof that a site
is supported: capability and authorization checks belong in a future adapter.

Changes to schemas or delivery policies need an explicit migration/recovery plan.
CI validates both Windows and Linux using pinned action revisions. The lockfile is
committed; update dependencies intentionally and rerun the full checks.

Contributions are licensed under Apache-2.0. Keep third-party notices and licensing
requirements when incorporating external code. This scaffold links to tools rather
than vendoring their implementations.
