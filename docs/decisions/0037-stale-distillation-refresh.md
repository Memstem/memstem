# 0037 — Staleness-driven distillation refresh (delta-gated)

Status: **Accepted — implemented**
Date: 2026-08-24
Related: 0020 (session distillation writer), 0023 (in-daemon hygiene loop)

## Context

ADR 0020 shipped the session-distillation writer with a deliberately
simple candidate filter: a session is skipped when a linked
`type: distillation` record already exists. The ADR named the
consequence under Cons:

> Stale distillations: if a session is re-emitted with materially
> changed content, the existing distillation is now wrong. v1
> mitigates via `--force`; a smarter staleness detector is a
> follow-up.

That follow-up was never built. Meanwhile the adapters re-ingest a
session's transcript continuously, so a long-running or reopened
session diverges from its one-shot summary — and because
distillations are deliberately boosted in search (importance seed
0.8 plus the per-type weight, per ADR 0020), the *stale summary
outranks the fresh transcript*. A user (Brad Snape, 2026-08-24)
independently diagnosed exactly this from observed recall lapses:
the summary is written once, nothing ever asks "has enough new
dialogue happened?" a second time.

A pure time-interval recheck (re-distill open sessions every N
minutes) was considered and rejected: the summarizer shares the
fleet's GPU LLM backend, and unbounded intermittent LLM jobs on that
box have already been removed once for contention with no offsetting
benefit (ADR 0028). Cost must be gated by *content*, not wall clock.

## Decision

**The default (non-`--force`) candidate scan re-distills a session
whose existing distillation is stale, gated on content delta.**

1. **Record source metrics at write time.** Every distillation's
   `provenance` now carries `source_word_count`, `source_turn_count`,
   and `source_updated` — a snapshot of the transcript the summary
   was generated from. `Provenance` is `extra="allow"`, so this is
   not a schema change.

2. **Delta gate.** A distilled session becomes a candidate again when
   the transcript has grown by at least
   `DEFAULT_REDISTILL_MIN_NEW_WORDS` (500) words **or**
   `DEFAULT_REDISTILL_MIN_NEW_TURNS` (10) turns since the recorded
   snapshot — i.e. the *new* content alone would roughly clear the
   ADR 0020 meaningfulness threshold. Both knobs are CLI-exposed
   (`--min-new-words`, `--min-new-turns`).

3. **Legacy distillations** (written before this ADR, no recorded
   snapshot) are treated as stale when the session's `updated` is
   newer than the distillation's `updated`. They earn exactly one
   snapshot-stamping refresh; every later refresh is delta-gated.
   The blast radius is bounded by the existing recency window
   (30 days) and the per-cycle cap, so a fleet deploy converges
   gradually instead of re-running the LLM over the whole vault.

4. **Never-distilled sessions rank first.** Candidates are ordered
   fresh-sessions-then-stale-refreshes, so under
   `distill_max_per_cycle` a backlog of refreshes cannot starve
   first-time distillation of new sessions.

5. **Refresh reuses the `--force` write path** — same record id,
   overwrite in place, re-enqueue for embedding — with one fix:
   the refreshed record now preserves the original `created` and
   bumps only `updated` (previously a `--force` rewrite reset
   `created`).

6. **Off switch.** `--no-refresh-stale` restores the old
   existence-only filter. The daemon default is on.

## Consequences

- A pinned/open session's summary now follows the transcript at
  meaningful-growth boundaries; a reopened old session (within the
  recency window) is re-summarized on its next qualifying delta.
- Refresh failures ride the existing empty-summary retry cap and
  TTL (the prior distillation stays in place — safe failure mode).
- LLM cost is bounded: per-cycle cap unchanged, and a session can
  trigger at most one refresh per qualifying delta because each
  rewrite re-snapshots the source metrics.
- The summarizer prompt cache naturally misses on refresh (the
  prompt embeds the grown transcript), so no cache invalidation
  work is needed.
- `hygiene/verify.py`'s use of `find_distilled_session_ids` is
  unaffected (existence semantics unchanged).
