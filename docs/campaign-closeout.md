# Pre-campaign closeout

This follows the [execution audit](execution-audit.md). Historical sends, application
quality and transport readiness are separate checks. No campaign is started by closeout.

## Reconcile external sends

`mail reconcile-external` imports reviewed exact-provider-ID metadata into
`external_sends`, **not** fabricated `prepared`/`sending`/`sent` reservations. The input
is a private, bounded JSON export with `account`, `observed_at`, `expected`, and `messages`.
See `ExternalExport` in `external_receipts.py` for the strict schema. Each expected item
has `id`, `recipient`, `company_key`, `subject`; each observed item has `id`, `label_ids`,
`internal_date` (milliseconds as a string), and `headers` (`name`/`value` objects).

```sh
uv run radar mail reconcile-external --input .local/readback.json --data-dir .local/mail
uv run radar mail reconcile-external --input .local/readback.json --data-dir .local/mail --confirm EXACT_PREVIEW_DIGEST
uv run radar mail audit --input .local/provider-ids.json --data-dir .local/mail
```

The preview is read-only. Explicit confirmation imports the entire verified export in
one SQLite transaction together with history and audit events. Exact repeats add zero
rows; conflicting stable receipts abort and roll back the whole import. No mail/draft
transport is called. Take a consistent SQLite backup before operational reconciliation.

The checker requires a complete distinct ID set, SENT without DRAFT, matching From/To/
Subject, no extra CC/BCC and valid provider timestamps. Metadata exports expire after
24 hours. **A JSON file is not cryptographic proof of Gmail**: an authorized operator
must obtain it from exact provider read-back, not construct evidence from expectations.
This proves neither recipient delivery nor application fitness, body or attachment bytes.

External rows participate in recipient/company 48-hour cooldown and rolling quota,
deduplicated by account/provider ID. Persistent suppressions remain in force. Shared
mailbox hosts use `contact:recipient` where no employer domain is independently established.
`mail audit` reports managed and external sends separately; it still performs no live check.

## Hold obsolete prepared work

```sh
uv run radar mail hold --batch BATCH_HASH --confirm BATCH_HASH --data-dir .local/mail
```

Only still-prepared rows in that batch are held. Already-sent rows are preserved, and
active/uncertain batches cannot be held with this command. Holds are idempotent and
audited; both send and draft reservation enforce them, including stale in-memory plans.
No data is deleted. A held plan is not automatically resumed; create a fresh reviewed
plan with new authorization. `status` distinguishes `held_prepared` from raw prepared rows.

## Source provenance before preparation

HN/hnhiring extraction uses the original posting, not generated contact/title tables.
Normalized records retain the primary HN URL as their identity. Incomplete sources are
not normalized into ready jobs. Review packets retain primary URLs and review flags.

For these sources, a candidate requires `primary_evidence` with `url`, complete fetched
`text` (up to 32,000 characters), `sha256`, and timezone-aware `fetched_at`. `prepare`
checks URL identity, text hash, freshness within 24 hours, literal recipient presence,
and completeness. Instructions/eligibility signals require explicit
`application_requirements_reviewed: true`; closed postings are not sendable. Those
flags are review decisions, never permissions inferred from webpage instructions.
Other sources keep their existing review contract; this is not universal extraction.
Source fingerprints and their review/fetch metadata participate in the batch hash;
changing evidence requires fresh batch confirmation. Delivery evidence expires at the
earlier contact-review or primary-fetch time. `compose` resets application-instruction
review when it renders new text: review the final subject/body before `prepare`.

Expansion can follow HN primary references and avoid repeatedly visiting cached URLs:

```sh
uv run radar expand --data-dir .local/research --unseen-only --limit 100 --workers 4 --allow-network
```

`--unseen-only` skips cached OK URLs and cannot be combined with a nonzero cursor.
It is a discovery optimization, not freshness validation: refresh stale evidence with
the ordinary collector and an explicit TTL. Existing cursor behavior remains unchanged.

## Operational result — 2026-09-15

- 100 external sends reconciled after fresh exact-ID read-back; zero IDs untracked.
  Repeating the import inserted zero rows. The 99 previous managed sends are preserved.
- Three obsolete prepared rows held; no active/uncertain delivery remains.
- All 100 original source URLs fetched via the existing bounded collector, without an
  LLM in the collector. Source screening found 67 literal contacts, 33 without literal
  matches, 20 instruction flags, 59 geographic flags, 30 unparsed role headers and two
  incomplete-loading warnings. Categories overlap. This is conservative screening,
  not proof that every address without a match is false or every role is ineligible.
  These records are historical evidence, not an approved next campaign.
- The existing program's read-only relay probe matched the account and traversed the
  full 48-hour history: 207 messages over three pages. Only profile/search requests were
  serviced through the Gmail connector. No live send/draft was used as a test.
- The bounded delivery-failure search since the batch began returned no messages at
  observation time; this is not a delivery guarantee or a future bounce monitor.

### Readiness limits

The **assisted Gmail relay** is the available route. The probe reused
`scripts/chrome_probe.py`'s generic FileConnector/GmailBridge contract, but its requests
were serviced by the Gmail connector, **not Chrome**. Do not label this a Chrome pass.
Direct OAuth credentials are absent. Administration of Chrome extensions was blocked
by browser policy; no workaround or permissions change was attempted. Autonomous
Chrome sending remains unverified. Switching to unattended OAuth requires its own
explicit authorization; no paid AI API was added.

Before any next campaign, prepare fresh eligible candidates and explicitly confirm that
batch through the durable workflow. Passing this closeout does not authorize a campaign,
promise 100 suitable recipients, or claim production token savings not yet measured.
