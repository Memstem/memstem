# 0027 — Transient summarizer failures must not poison the distill-retry cap

Status: **Accepted — implemented**
Date: 2026-06-27
Supersedes: none
Related: 0020 (session-distillation writer), 0023 (in-daemon hygiene loop)

## Context

Session distillation (ADR 0020) caps per-session retries: after
`DEFAULT_MAX_DISTILL_ATTEMPTS` (3) empty summaries a session is excluded from
future cycles, tracked by a `distill_fail:<id>` row in `hygiene_state`. The cap
exists to stop a genuinely-unsummarizable session (an oversized transcript that
still 400s, content the model won't summarize) from burning a summarizer call
every tick.

The bug: the summarizer ([core/summarizer.py](../../src/memstem/core/summarizer.py))
caught **every** exception — connection-refused, timeouts, 5xx included — and
returned the empty string, identical to "the model produced no usable text." The
distill applier counted each empty toward the cap. So a **transient backend
outage** — the LLM sidecar down, a Cloudflare-tunnel 530, a momentary 500 —
permanently excluded every session it caught mid-distillation, with no
self-recovery (the only cap reset fires on a *successful* write, which an
excluded session can never reach).

This caused the June 2026 fleet stalls: E1 lost 8 days of distillation behind a
stopped `:9444` sidecar; techpro (`distill_fail:18`) and cargol
(`distill_fail:2`) were poisoned by brief tunnel blips. The embed path never had
this problem — it retries transient failures indefinitely (`retry_count
unchanged`). Distillation lacked the same grace.

## Decision

1. **Classify summarizer failures.** `Summarizer.generate` raises a new
   `TransientSummarizerError` on a retryable backend failure
   (network / timeout / 5xx / 429 / 408) and still returns `""` only when the
   model genuinely produced no usable text (NoOp, an empty 200, or a permanent
   4xx). `generate_cached` propagates the transient error rather than caching it
   or collapsing it to `""`.

2. **Skip-without-recording on transient.** `compute_distillation_plan` catches
   `TransientSummarizerError`, skips the candidate this cycle **without** emitting
   an (empty) proposal — so `apply_distillations` records no failure and the cap
   is untouched. The session stays eligible and retries next cycle, mirroring the
   embed worker. Surfaced as a new `skipped_transient` plan stat.

3. **Cap TTL (defense in depth).** A capped session is excluded only for a
   cool-down (`DEFAULT_DISTILL_FAIL_TTL`, 24h), tracked by a companion
   `distill_fail_at:<id>` timestamp row. After the cool-down it earns one more
   attempt; success clears the record, another failure restarts the cool-down.
   Legacy `distill_fail` rows with no companion timestamp are treated as
   cooled-down, so caps already present in production self-heal on upgrade.

`hygiene_state` is non-canonical (like the embed queue), so this is not a
storage-invariant change; the canonical markdown store (ADR 0002) is untouched.

## Consequences

- A momentary backend outage no longer silently kills distillation — sessions
  retry until the backend recovers, so the failure mode that hid for 8 days on
  E1 cannot recur from a transient cause.
- A genuinely-unsummarizable session is retried at most ~once per TTL window
  (not every tick), bounded further by the recency window (ADR 0020). Its
  attempt counter grows unbounded but is harmless.
- `get_distill_failures` keeps its count-only return for back-compat;
  `get_distill_failure_records` exposes the `(count, timestamp)` pair.
- Operational detection (the `memstem-health` probe's distill-failure check) and
  a stop-gap cron that clears stale caps complement this — but the in-code fix
  is the durable one.
