# ADR 0058: Write-time filters keep substantive records; outages don't fail saves

Status: proposed. Date: 2026-11-27. Amends ADR 0011 (noise filter) and the
embedder error classification.

## Context

ADR 0011's write-time noise filter drops heartbeat output, cron output and tool
dumps, and stamps a 28-day expiry on "transient task" statements. Each rule
looks at a single line or a line ratio, then acts on the whole record:

- **heartbeat / cron:** one line that is exactly `HEARTBEAT_OK`, starts with
  `[heartbeat]` or `Running cron job:`, or contains an `__openclaw_*dream__`
  marker drops the record.
- **tool_dump:** 80% or more of the non-empty lines look machine-made (JSON
  braces, quotes, tool markers) drops the record, however much prose the rest
  holds.
- **transient_task:** a phrase like "merge the fix by tomorrow" anywhere sets
  `valid_to = now + 28 days` at every save. The record vanishes from search 28
  days after its last edit. The expiry was also carried forward on every later
  save, so editing the phrase out did not lift it.

Separately, the embedder treated every 4xx reply as permanent. A failover
sidecar answers 429 when every route is down, so during a provider outage of a
few minutes a record that was saved once (a session that ended during the
outage) used up its five retries and stayed `failed` until someone ran
`memstem embed --retry-failed`.

Measured on the maintainer's install (2026-11-27), replaying every source on
disk through the filter and reading the live index:

| Rule | What it hid | Genuine noise among them |
|---|---|---|
| heartbeat | 6 of the agent's daily notes (11-70 KB each; 5 never indexed, 1 frozen at 44 of 70 KB) because each logs a heartbeat run among the day's notes; 3 session-cache notes | none |
| tool_dump | 1 Claude Code conversation (48,000 characters of prose beside a pasted 3,000-line settings file), 1 Codex conversation (25,000), 2 Codex review runs (4,000 and 16,000) | none |
| transient_task | 7 Claude Code conversations (5 already hidden, including a 684,000-character project-status session) and 4 session-cache notes | none |
| heartbeat, OpenClaw transcripts | 1 scheduled heartbeat run | all |

The rules that should catch noise still do: the agent's heartbeat and cron runs
are its own OpenClaw session transcripts, and the scheduled-session rule (ADR
0049) expires the rest.

## Decision

1. **Substantive records are kept.** A record with at least 1,000 characters of
   prose outside machine, heartbeat and cron lines (role prefixes such as
   `**User:**` and tool markers such as `[tool_use: Bash]` are not prose) is not
   dropped by the heartbeat, cron or tool-dump rules. 1,000 characters is about
   150-200 words; every wrongly dropped record above had 4,000 or more, and a
   heartbeat or cron run's own output, with its marker lines removed, is
   typically well under it.
2. **OpenClaw agent transcripts keep the strict rule.** In an OpenClaw session
   transcript (`source: openclaw`, type `session`) a heartbeat or cron marker is
   the agent's own run, so the whole transcript is that run and is dropped as
   before. Elsewhere (notes, memory files, Claude Code, Codex and OpenCode
   sessions) a marker is quoted material: a logged heartbeat, a pasted log.
3. **The transient-task rule is retired.** It is no longer applied at ingest.
   On the measured install it caught nothing but real conversations and notes,
   and a deadline in a standing to-do list is a reason to keep it findable, not
   to hide it four weeks after its last edit. `is_transient_task` stays
   exported. Path-based `automation_log` and the ADR 0049 scheduled-session
   expiry are unchanged.
4. **Filter expiries are stamped and lift themselves.** When the filter sets
   `valid_to` it also writes `provenance.noise_expiry: <kind>`. On a later save
   that the filter no longer tags, a stamped expiry is dropped and the record is
   re-queued for embedding (the expired-vectors pass may have stripped its
   chunks while keeping `embed_state`). An unstamped expiry (set by hand or by
   `cleanup-retro`) is preserved as before.
5. **Old expiries are lifted by an explicit pass.** `memstem hygiene
   lift-noise-expiry` (dry-run by default, `--apply` to write) lists unstamped
   expiries whose body matches one of the old rules and which the current filter
   keeps, then clears them and re-queues their embeds. Records matching none of
   the old rules are never touched. On the measured install it finds 11 records
   (the 7 conversations and 4 notes above) and leaves the 8,584 scheduled-session
   expiries alone.
6. **"Try again later" replies are transient.** HTTP 408, 425 and 429 from an
   embedding provider raise `TransientEmbeddingError`, like 5xx and network
   errors: the worker backs off and retries without spending the record's retry
   budget, and the circuit breaker counts them. Other 4xx replies stay
   permanent.

## Consequences

- Records the filter dropped come back on the next reconcile; nothing else is
  needed. Expired ones need the `lift-noise-expiry` pass once per install.
- A long-running quota exhaustion (a 429 that never clears) now keeps records
  pending instead of failing them. The pending count in `/health` grows, which
  is the signal to act on; previously the same situation showed as `failed`.
- A short note that is nothing but a heartbeat log with under 1,000 characters
  of other prose is still dropped. A pure heartbeat log kept under a
  `heartbeat/` or `monitoring/` path is still tagged transient by path.
- Search, ranking, storage format and the adapter interface are unchanged.
  `provenance.noise_expiry` is an optional field (`Provenance` allows extras).

## Verification

A scratch test (real adapters, pipeline, embed worker and search against a
throwaway vault and a fake provider) covers: a long Claude Code session with an
overload error and a usage-policy refusal mid-way, saved during a 12-request
429 outage; a session with a pasted 3,000-line JSON file; a session with a
pasted PM2 log containing heartbeat lines; a daily note that logs heartbeat
runs; a standing to-do list with "by Friday" last edited 29 days ago; a session
saying "merge ... by tomorrow" 29 days ago; and editing the deadline out. On the
code before this change 11 of 15 checks fail; after it all 15 pass. Unit tests
pin each rule, the stamp-and-lift path, the preserved unstamped expiry, the
lift pass and the 408/425/429 classification.
