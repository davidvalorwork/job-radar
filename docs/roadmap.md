# Delivery roadmap

No milestone below implies production readiness before its acceptance gates pass.
The default path must run locally with no paid API or cloud-model calls. Development
assistant usage, electricity, hardware, and provider limits are separate from that
runtime design; this is not a promise of unlimited free delivery.

## 0. Bootstrap — implemented

- Hexagonal package, strict YAML, typed ports and composition root.
- 27-source planning catalog, DevOps-first / Full Stack AI profiles.
- Bounded offline query planner and streaming synthetic JSONL import.
- Deterministic matching, idempotent job storage, atomic audit and config hashes.
- CLI diagnostics, JSON completion logs and Prometheus text snapshots.
- Windows/Linux CI, lint/type/architecture/unit/integration checks.

## 1. Unified discovery adapter

Use Agent Reach as the capability/discovery guide and OpenCLI as the shared scripted
surface, without a custom API client per job board. Probe installed command manifests
and authorized sessions before dispatch. Represent unsupported/expired/login-required
states explicitly; do not report every catalog entry as connected.

Execute allowlisted argument arrays (`shell=False`), parse bounded structured
outputs, apply deadlines, cap output bytes and clean up process trees. Lock sessions;
respect access restrictions and stop on challenges. Distinguish search from read:
web-domain queries find URLs; page readers normalize job pages afterward.

Acceptance: fixtures for capability drift, nonzero exits, timeout, corrupt/oversized
output, revoked sessions, blocked sources and schema drift; small authorized smoke
tests only. Pin compatible versions after real capability checks.

## 2. Durable phased workers

Add per-phase tasks, dedup keys, leases/heartbeats, run cursors, retry counters and
dead-letter reasons. Recover expired leases only for retry-safe work. Retry transient
failures with bounded exponential backoff, jitter and Retry-After; isolate sources
using circuit breakers. Keep business/audit writes atomic.

Start with resource measurements from `psutil`: async subprocess I/O, per-source and
global semaphores, bounded queues, one writer, adaptive worker reductions on memory
pressure or throttling. Introduce process pools only after measuring CPU saturation.

Acceptance: interruption/restart, duplicate workers, SQLite contention, failed
checkpoints, full disk, source throttling and predictable shutdown tests. Benchmark
useful qualified results/minute and memory, not just raw fetch count.

## 3. Configurable policy and controlled relaxation

Add explicit precedence `global -> role -> source -> target`, with effective-policy
hashes per task. Catalog selectors cover category, source, page/group/feed target,
tags, enabled/capability/access status and source budgets.

Candidate filters: role priorities/aliases, required/excluded terms, seniority,
remote/hybrid/on-site, permitted geography/time zone, languages, contract type,
salary range/currency and unknown-salary policy, publication/discovery age, verified
company/domain, contact evidence and confidence. Do not silently infer eligibility.

Relaxation ladder: broaden query synonyms -> add permitted sources/targets -> widen
freshness -> relax explicitly soft preferences. Give each tier a minimum result
target, time/query/page budget and stop condition; record the before/after policy and
reason. Never relax opt-outs, confirmed bounces, access controls, recipient cooldown,
unresolved sends or required authorization. Avoid repeating equivalent queries.

Acceptance: deterministic tier progression, hard-vs-soft policy tests, unknown-value
handling, replayable provenance and no unbounded search when results are scarce.

## 4. Separately authorized outreach

Prepare factual templates from verified job/company/contact evidence. Never invent
experience, contact addresses or personalization. Start with draft review before a
separately enabled sender. A Gmail integration is not implemented or assumed ready.

Desired configurable target: up to **100 emails per rolling 24 hours**, limited by
provider rules and available eligible opportunities. Reserve quota transactionally
across workers; include imported recent sent history. A 48-hour recipient/company
cooldown does not suppress all old Gmail matches forever. Expiry never schedules an
automatic resend. Persist suppression independently.

Delivery states need `prepared -> approved -> reserved -> sending -> sent`, plus
`failed`, `unknown` and `suppressed`. After an ambiguous timeout, reconcile with the
provider instead of resending. Do not claim exactly-once email delivery.

Acceptance: quota races, duplicate recipients/company identities, opt-outs, bounces,
history gaps, ambiguous sends and provider throttling. No live sending in CI.

## 5. Deeper observability when justified

Extend bounded-label metrics: phase durations, queue age, source yield, rejection
reasons, timeout/retry rates, resource pressure, cooldown/quota suppression and
reconciliation lag. Add local OpenTelemetry traces/metrics and an optional Collector
+ Grafana stack after workers exist. Keep business audit separate from sampled traces.

Raw browser traces and contact data stay local with access/retention limits. Disable
remote exports by default, redact at ingestion, and test labels/logs for PII. Avoid
always-on infrastructure that costs more CPU/RAM than the actual discovery work.
