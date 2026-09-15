# Security and privacy

This is an early implementation, not a production-certified bulk sender. Planning
and import remain offline. Only explicit `collect --allow-network` sends public
queries/URLs to Exa/Jina. Mail uses separately authorized Gmail OAuth or a legacy
host connector relay. See [mail operations](docs/mail.md) and [setup](docs/autonomous.md).
OAuth files and cached raw evidence must remain private and outside Git. The direct
adapter requests read/send scopes, not delete/modify scopes. Tokens are not encrypted
on disk; protect them with filesystem permissions, including Windows ACLs.

The relay directory contains private message bodies, CV bytes and mailbox responses.
Keep it on a trusted local disk, out of Git, and accessible only to the authorized user.
A relay must verify request expiry and allowlisted methods, persist a dispatch marker
before any send, dispatch each ID only once, and write replies atomically. Never
recover a missing send response by redispatching the request. Local filesystem admins
can modify relay files and databases; this is not a hostile multi-tenant boundary.

## Reporting

Use GitHub private vulnerability reporting if enabled. Otherwise open an issue
requesting a private reporting channel without disclosing exploit details, secrets,
personal data, or real contacts. Do not attach a local database to an issue.

## Local data

- `.local/`, `.codegraph/`, environments, databases, cookies and local config are
  ignored by Git. Ignore rules are a guardrail, not a secret scanner.
- The local SQLite database contains job text, identifiers, timestamps and decisions.
  It is **not encrypted**. Protect filesystem access and backups yourself.
- Audit is transactional but is **not tamper-proof** against a local administrator.
- Logs contain run IDs and counters; errors do not echo imported records. Metrics
  use bounded decision labels, never recipient, company or URL labels.
- Configuration is trusted local input, not an uploaded multi-tenant API. YAML is
  bounded to 1 MiB, safe-loaded, and rejects aliases and duplicate keys.
- JSONL is bounded to 8 MiB total and 64 KiB per line. Unknown fields, invalid dates,
  unregistered sources and embedded URL credentials are rejected.
- URL normalization establishes identity only. It does **not** make a URL safe to
  fetch. Future networking must handle private IPs, DNS rebinding, redirects, access
  permissions and resource limits explicitly.

## Future networking and contact delivery

Require permitted sources and user-authorized sessions. Respect site limits and
access controls; never bypass CAPTCHA, rotate accounts to evade limits, guess
addresses, or harvest email addresses from Git commits.

The 48-hour contact rule is a cooldown, not permission to repeatedly contact someone.
Suppress explicit opt-outs, hard bounces and known spam rejections. Unknown delivery
outcomes need reconciliation, not blind retries. Rolling-volume quotas cannot
override provider limits or recipient restrictions. Discovery qualification alone
must never trigger an external message.
