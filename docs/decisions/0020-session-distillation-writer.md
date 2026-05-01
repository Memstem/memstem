# ADR 0020: Session distillation writer

Date: 2026-05-01
Status: Accepted

## Context

ADR 0008 Tier 2 specified the distillation pipeline in two flavors:
session distillation (one session → one summary) and topic
distillation (cluster of related memories → one rollup). The
*candidate report* slice shipped (`hygiene/distillation.py`,
`memstem hygiene distill`) — deterministic clustering by `topic:*`
tag and by daily-log + ISO-week, no LLM, no vault mutation.

The *writer* slice — the part that takes a candidate or a session and
actually produces a `type: distillation` record — has not shipped.
That gap is now blocking real recall quality:

> A search for *"Woodfield Country Club e-bike video"* should surface
> a project-shaped, retrieval-optimized record. Today, the underlying
> session JSONLs exist in the vault but rank weakly (vec_rank 3, score
> 0.017) because the raw transcripts are conversational —
> verbose, full of tool calls, with the answer scattered across hours
> of back-and-forth. The data is there; the *shape* is wrong.

ADR 0019 ruled out skill authoring: each AI generates skills its own
way; MemStem ingests `SKILL.md` files but does not write them. The
ADR's body text reads strictly as "no LLM-authored new vault records,"
but the explicit list of removed work only mentions PR-G (auto-skill
extraction) and W7 (reflective synthesis). It also explicitly retains
ADR 0008's PRs A–F, which include session and topic distillation.

This ADR resolves that internal inconsistency for the session-
distillation case: **session distillations are in scope**, with the
boundary described below.

## Decision

Ship the session distillation writer as the next slice of ADR 0008
PR-D. A session record that meets the meaningfulness threshold gets a
companion `type: distillation` record produced by an LLM call to a
configurable summarizer. The distillation links back to its source
session via the existing `links` frontmatter field; the source session
is preserved unchanged.

### What this is, what this isn't

**This is** the pattern ADR 0008 always intended: derived,
provenance-pointing summaries of source records. Every distillation
points back at exactly one session (the session-distillation case
here) or at a cluster of source memories (the topic-distillation
case, future PR). No distillation "stands alone" — the source is
always one click away via `links`.

**This is not** skill authoring per ADR 0019. The boundary:

| Aspect           | Skill (ADR 0019, out of scope)                        | Session distillation (this ADR, in scope)                       |
|------------------|-------------------------------------------------------|-----------------------------------------------------------------|
| What it produces | A reusable procedure                                  | A summary of one specific session                               |
| Format           | Per-AI conventions (Claude Code, Codex, Hermes, …)    | MemStem-internal `type: distillation`                           |
| Provenance       | Often unstated; the procedure is the artifact         | Mandatory: `links` + `provenance.ref` point at the source       |
| Standalone use   | Yes — agents act on the procedure directly            | No — distillation is a retrieval shortcut to the source         |
| Coupling risk    | Couples MemStem to whichever skill format it picks    | Internal to MemStem; no AI consumes it as authoritative content |

A skill says *"here is how to do X."* A session distillation says
*"this session was about Y; here are the entities, decisions, and
artifacts; the full transcript is at this link."* The latter is a
search-shaping operation on existing content, not a knowledge claim.

### Scope (v1)

In scope for this ADR:

- One distillation per "meaningful" session, generated on-demand by
  CLI and idempotent across re-runs.
- Backfill mode that summarizes all eligible existing session records
  in the vault (one-shot operation; no live system contact required).
- Pluggable summarizer (NoOp default, Ollama, OpenAI), mirroring the
  rerank / HyDE / dedup-judge pattern from ADRs 0012/0017/0018.
- Provenance and link-back so a search returning a distillation can
  click through to the verbatim transcript in one hop.
- Search-ranking integration via the existing importance multiplier
  (ADR 0008 Tier 1) — distillations are seeded with `importance: 0.8`
  so they outrank raw sessions on close ties without forcing their
  way past clearly-better matches.

Explicitly out of scope for v1 (deferred to follow-ups):

- Auto-trigger from the daemon's ingestion pipeline. The summary
  command is CLI-driven; an optional daemon hygiene tick is left
  documented as a follow-up under the existing `memstem.hygiene`
  package.
- Topic distillation writer (ADR 0008 PR-E). The candidate report for
  topics already ships; a follow-up ADR can land the writer for
  those clusters when needed.
- Multi-pass refinement (re-summarize when the source session
  materially changes). v1 detects change via body hash and overwrites
  on `--force`; a separate ADR can specify a smarter "re-summarize on
  significant edit" policy if needed.
- Bidirectional cross-ref (the source session gaining a
  `distilled_by:` field). v1 keeps the link one-way from the
  distillation, per ADR 0008's "Open questions" preference for the
  simpler path.

### Meaningfulness threshold

A session is eligible for distillation iff:

1. `metadata.turn_count >= 10` (or, when missing, body word count
   >= 200), **and**
2. The body is not a system-prompt boot echo (already filtered at
   ingest by ADR 0011 PR-C; we re-check defensively), **and**
3. The body word count is at least 100 (so a 10-turn session of
   one-line replies still gets skipped — the typical "hi ari, what
   time" / "thanks" / "ok" exchange).

Both gates are deterministic and config-tunable
(`hygiene.distillation.min_turns` etc.). The default values target
"this session was real work" without burning LLM budget on chatter.

### Output shape

Distillations land at
`vault/distillations/<source>/<session_id>.md` (or
`vault/distillations/<source>/<agent>/<session_id>.md` when an
agent tag is present). The frontmatter:

```yaml
---
id: <uuid>
type: distillation
title: "Woodfield Country Club — e-bike & golf cart proposal review"
created: 2026-05-01T03:00:00Z
updated: 2026-05-01T03:00:00Z
source: hygiene-worker
provenance:
  source: hygiene-worker
  ref: "session-distillation:<source-session-id>"
  ingested_at: 2026-05-01T03:00:00Z
links:
  - "memory://sessions/b7972233-e434-42fb-b55a-1736bd17e211"
tags:
  - "agent:ari"                    # inherited from source
  - "home-ubuntu-woodfield-quotes" # inherited from source
  - "distillation:session"
importance: 0.8
---
```

The body is a 1-paragraph summary plus a structured "Key entities /
deliverables / decisions" section. The exact prompt template lives
in `prompts/distill_session.txt` and is the load-bearing piece for
recall quality — the LLM call is short, predictable, and easy to
iterate on.

### Idempotence + backfill

- Re-running `memstem hygiene distill-sessions` is a no-op on
  sessions that already have a linked distillation, regardless of
  `--apply`. The link discovery walks `type: distillation`
  frontmatter for `links` pointing back at session ids.
- `--backfill` widens the candidate set from "sessions ingested in the
  last N days" to "every session in the vault." Same idempotence
  guarantee.
- `--force` regenerates a distillation even if one exists — used when
  the source session has changed materially or when the prompt
  template has been improved.

### LLM choice

- **Default (NoOp).** No LLM dependency at install time. The
  distiller can be wired up explicitly via config, mirroring the
  rerank/HyDE pattern. NoOp returns the empty string; the writer
  detects this and skips the candidate (logged at INFO).
- **OpenAI.** Default `gpt-5.4-mini`. Recommended in
  `docs/recall-models.md` because the summary text *is* the search
  target — quality matters more than for rerank/HyDE.
  Cost at MemStem-scale workloads: ~$0.01 per session, dominated by
  input tokens (typical session ≈ 15k tokens in, 500 tokens out).
- **Ollama.** Default `qwen2.5:7b` for parity with the existing
  recall-quality features. Local, free, lower quality than 5.4-mini
  on benchmark summarization.

The model identifier follows the existing `provider:model` shape so
distillation cache rows (`distillation_cache`, schema migration
v11) don't cross-contaminate across providers.

## Schema additions

| Field             | Type            | Description                                                                                                                                                            |
|-------------------|-----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| (none)            | —               | The `distillation` type and `links`/`importance`/`provenance` fields all exist already (ADR 0008). This ADR adds zero new fields to the canonical frontmatter schema.  |

Internal SQLite migrations:

- `v11`: `distillation_cache(source_session_id PRIMARY KEY,
  summarizer, body_hash, distillation_id, ts)` — keyed on the
  source session id so re-runs short-circuit without walking
  frontmatter; `body_hash` lets `--force` invalidate when the source
  changes; `summarizer` keeps cache rows isolated per provider.
  Non-canonical (drop-and-rebuild safe).

## Search ranking

No code changes to `core/search.py`. The existing importance
multiplier (`final = rrf * (1 + alpha * importance)`, alpha=0.2)
already handles distillation prioritization once the seed value is
high enough.

The seed for distillations is **0.8** by convention (set in the
writer, persisted to frontmatter). Combined with alpha=0.2, that
boosts a distillation's RRF score by 16% — enough to beat a raw
session of similar relevance, not enough to surface an unrelated
distillation over a directly-matching skill or decision.

When the writer processes a session whose distillation already exists
and is being regenerated (`--force`), the importance value is
preserved (so manual `pin` and live-traffic bumps from ADR 0008 PR-B
aren't clobbered).

## Implementation phasing

This ADR is one slice (the writer); the other slices ship as
sequential PRs:

1. **PR-B: Summarizer abstraction.** `core/summarizer.py` mirrors
   `core/hyde.py`: `Summarizer` ABC + `NoOpSummarizer` +
   `StubSummarizer` + `OllamaSummarizer` + `OpenAISummarizer`,
   prompt template at `prompts/distill_session.txt`. Cache helpers
   on `distillation_cache` (schema migration v11). No CLI yet.
2. **PR-C: Session distillation writer.** `hygiene/session_distill.py`
   adds `find_session_candidates(vault, …)`, `distill_session(memory,
   summarizer)`, `apply_distillations(vault, plan)`. CLI command
   `memstem hygiene distill-sessions [--backfill] [--apply]
   [--force]` (lives next to existing `hygiene distill`).
3. **(separate ADR) PR-D: Project records.** ADR 0021 covers it —
   project record writer is conceptually distinct (project = collection
   of sessions sharing a project tag), but reuses the same summarizer
   abstraction.

## Rationale

- **Provenance preserves the architectural property.** Every
  distillation has a one-click route to its source session. No
  knowledge "exists only in the distillation" — the source is always
  authoritative.
- **CLI-driven, not pipeline-coupled.** The daemon stays
  network-free in its default loop (consistent with ADR 0019's
  preference for opt-in LLM features). Brad runs distillation as a
  scheduled command or on-demand; the embed-worker pattern can layer
  on later if real-time distillation becomes valuable.
- **Per-session, not per-cluster.** This ADR ships the simpler shape
  first because it's both easier to evaluate (one input → one output)
  and directly motivated by the recall failure ("the Woodfield
  session itself should be findable as a project-shaped record").
  Topic distillation can layer on top once we have empirical data.
- **Threshold over filter.** The meaningfulness gate uses a
  conservative threshold (10 turns + 100 words) rather than a smart
  classifier. Both numbers are config-tunable. A simple threshold is
  inspectable; a classifier is not.
- **Importance value as a tuning knob, not a search code change.**
  The seed 0.8 is the single number that determines how aggressively
  distillations float. We can tune it per-vault from the eval harness
  without changing search code.
- **Backfill via the same code path as live distillation.** The writer
  doesn't distinguish backfill from steady-state — both walk the
  vault, find session records without a linked distillation, and
  process them. This keeps the test surface small and avoids drift.

## Consequences

**Pros:**

- The Woodfield-style recall failure gets a direct fix: a project's
  worth of sessions becomes findable via a single distilled record.
- Search context is conserved: agents can pull a paragraph instead of
  a multi-thousand-token transcript.
- The pattern reuses existing frontmatter fields and existing
  importance ranking; no schema growth and no search code changes.
- Backfill is idempotent and re-runnable, so a soak run on Brad's
  vault is low-risk and reversible (every distillation is a separate
  markdown file; deleting them returns the system to pre-distillation
  state).

**Cons:**

- LLM cost scales with session volume. Bounded in practice (Brad's
  vault: ~356 sessions today, growing at ~5/day) but worth tracking
  via the eval harness output.
- Stale distillations: if a session is re-emitted with materially
  changed content, the existing distillation is now wrong. v1 mitigates
  via `--force`; a smarter staleness detector is a follow-up.
- One LLM provider's "summary style" leaks into the vault. Switching
  models mid-vault means inconsistent voices across distillations
  until a re-run with `--force`. Acceptable given this is a search
  optimization, not a knowledge artifact in its own right.

## Open questions resolved

- *"Trigger model"* — CLI-only for v1; daemon hygiene tick deferred.
  Rationale: keeps daemon network-free; LLM coupling is opt-in.
- *"What qualifies as a meaningful session?"* — 10 turns + 100 words.
  Tunable via `hygiene.distillation.min_turns` and
  `hygiene.distillation.min_words`.
- *"Default LLM"* — `gpt-5.4-mini` via `OpenAISummarizer`; Ollama
  `qwen2.5:7b` via `OllamaSummarizer`; NoOp default at install time.
- *"ADR 0019 boundary"* — distillations are search-shape derivatives
  with mandatory provenance, not skill authoring. The body text of
  ADR 0019 is amended via this ADR's boundary table; ADR 0019's "no
  skills" rule stands unchanged.

## References

- ADR 0008 — tiered memory; this is the writer slice of PR-D.
- ADR 0011 — atomic-fact extraction (the closest precedent for
  LLM-authored new records with provenance).
- ADR 0017 / 0018 / 0012 — the rerank / HyDE / dedup-judge pattern
  this writer mirrors (ABC + NoOp + Stub + Ollama + OpenAI, cache
  table, lazy httpx import, `provider:model` name).
- ADR 0019 — skill authoring out of scope; this ADR clarifies the
  boundary for distillation.
- ADR 0021 — project records (companion ADR; uses the same summarizer
  abstraction for a different output shape).
- RECALL-PLAN.md — the W8 work item this ADR formalizes.
- `docs/recall-models.md` — LLM-choice ladder for the summarizer.
