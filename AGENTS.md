# Job Radar contributor guidance

- Keep this repository generic: never commit real contacts, mailboxes, credentials,
  sessions, CVs, local paths, evidence, databases, or raw browser traces.
- Hexagonal dependency direction: domain <- application <- adapters; only bootstrap
  wires concrete adapters. Domain/application must not import infrastructure packages.
- Networking and sending are intentionally unavailable in the bootstrap release.
  Do not add hidden external side effects or change these defaults to make tests pass.
- Use injected clocks, typed ports, and synthetic fixtures. No live accounts in tests.
- Business state and audit events must commit atomically. An uncertain send must never
  become a blind retry; future delivery adapters require explicit authorization.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
  `uv run pytest` before publishing changes. Keep lockfile and package metadata aligned.
- The local CodeGraph index is ignored by Git. Use structural tools when available;
  otherwise inspect exact files and keep changes focused.
