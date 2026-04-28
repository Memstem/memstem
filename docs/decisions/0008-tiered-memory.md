# ADR 0008: Tiered memory — importance scoring, distillations, and hygiene

Date: 2026-04-25
Status: Proposed

## Context

Phase 1 ships a flat memory store: every adapted file becomes one
`Memory` record, indexed by FTS5 + sqlite-vec, retrieved via Reciprocal
Rank Fusion. On Brad's box that's ~940 records on day one — enough to
expose two limits:

1. **Search noise.** A query like *"what did we decide about pricing?"*
   retrieves 17 partial discussions across daily logs, session
   transcripts, and Ari's MEMORY.md. Hybrid ranking helps, but the
   results are still a stack of source material that the agent has to
   re-read every time. There is no concept of "this 2-paragraph rollup
   is the answer; the 17 sources are the citations."
2. **No self-improvement loop.** Records age in place. A skill Brad
   used 50 times last quarter scores the same as a one-off note. A
   superseded plan ranks alongside the current one. Search behavior on
   day 30 is the same as day 1; the system never gets smarter at
   surfacing what matters.

The flat-store design is correct for v0.1 — it is the smallest thing
that can replace FlipClaw — but Phase 2 needs a layer that captures
*importance* and produces *distillations* without breaking the storage
invariant ("markdown files are canonical, the index is rebuildable").

This ADR locks the v0.2 design before any code lands. PR-level work
will follow once Brad signs off on the shape.

## Goals

1. Rank skills, intentional notes, and frequently-retrieved memories
   above conversational noise without hiding the noise entirely.
2. Produce digest memories ("distillations") that are themselves
   first-class records — searchable, linkable, supersede-able — rather
   than ephemeral cache.
3. Keep the storage invariant intact: distillations and importance
   scores live in markdown frontmatter and `_meta/`-scoped tables,
   never as derived state the index couldn't rebuild.
4. Stay local-first by default. Distillation can call out to a
   higher-quality model if the user opts in, but the baseline runs
   against the same Ollama instance the embeddings use.
5. Never delete a raw record automatically. Decay reduces ranking;
   dedup creates a `deprecated_by` redirect. The user can always
   recover the original.

## Non-goals

- Changing the on-disk format of existing records. Frontmatter gains
  optional fields (`importance`, `valid_to`, `deprecated_by`,
  `supersedes`, `links`) — none break v0.1 readers.
- Distributed/multi-device concerns. ADR 0007 already gates that on
  Phase 4.
- Real-time distillation. The hygiene worker runs on a schedule
  (default: nightly), not on every write.
- Replacing the agent's judgment. Importance is a tiebreaker layered
  on top of hybrid retrieval, not a hard filter.

## Decision

Layer three tiers on top of the existing raw-memory store. Each is
additive: removing any one tier returns the system to a strict subset
of the previous behavior.

### Tier 0 — Raw memories (already in v0.1)

Every adapted file → one record. Source of truth, never deleted by
the system. ~940 records on Brad's box at cutover. No change in v0.2.

### Tier 1 — Importance scoring

Each memory carries an `importance: 0.0-1.0` field on its frontmatter
(the schema already permits it; v0.1 leaves it unset, which the
ranker treats as 0.5).

**Heuristic seed (computed at ingest, by the pipeline):**

| Signal           | Effect                                                              |
|------------------|---------------------------------------------------------------------|
| Type weight      | `skill` 0.7 / `decision` 0.6 / `memory` 0.5 / `session` 0.3         |
| Recency          | linear decay from 1.0 at creation to 0.5 at 90 days, constant after |
| Wikilink density | each `[[X]]` inbound from another memory adds 0.05 (capped at +0.3) |
| Length penalty   | bodies under ~100 chars drop 0.1                                    |

The intuition: skills are intentional learnings; sessions are
conversational and most of their content is incidental.

**Live boost from query traffic:**

A new `query_log` table in `_meta/index.db` records every
`memstem_search` result and every `memstem_get` open. The hygiene
worker periodically reads the log and bumps `importance` on memories
that appeared in successful retrievals — weighted by their rank in the
result list and by whether the user actually opened them.

**Manual pin:** `memstem pin <id>` locks `importance = 1.0` and
disables decay. `memstem unpin <id>` reverses it.

**Search ranking** changes from `rrf_score` to:

```
final_score = rrf_score * (1 + α * importance)
```

with `α = 0.2` so importance is a tiebreaker, not a forcing function.
A pinned memory effectively doubles its score.

### Tier 2 — Distillations (the "dreaming" pass)

The existing `memstem.hygiene` package (currently a stub) gains a
worker that periodically clusters related raw memories and asks an
LLM to summarize them. Output is a new memory with `type:
distillation` (a new value added to the schema enum).

**Distillation memory shape:**

```yaml
---
id: <uuid>
type: distillation
title: "Cloudflare decisions and migration plan"
created: 2026-04-26T03:00:00Z
updated: 2026-04-26T03:00:00Z
source: hygiene-worker
provenance:
  source: hygiene-worker
  ref: "topic-cluster:cloudflare"
  ingested_at: 2026-04-26T03:00:00Z
links:
  - "[[memory://memories/openclaw/abc-123]]"
  - "[[memory://memories/openclaw/def-456]]"
  - "[[memory://sessions/xyz-789]]"
importance: 0.8
---

Across 12 conversations and daily logs over March-April 2026 we settled
on Cloudflare for new domains because at-cost pricing saves ~$1,200/yr
across 100 domains. GoDaddy renewal pricing is 2x+. Migration plan...
```

**Two flavors ship first:**

1. **Session distillation.** Long Claude Code sessions (≥ 30 turns or
   > 10k tokens) get a 1-paragraph "decisions / learnings" note. The
   source session links back. An agent searching for *"what did we
   decide on Tuesday"* sees the distillation; if it needs the verbatim
   transcript, one click via `links`.

2. **Topic distillation.** Cluster raw memories by embedding
   similarity (cosine ≥ 0.7, min 5 members). For each cluster, write a
   "What we know about X" rollup. Run weekly; older distillations are
   superseded by newer ones via a `deprecated_by:` field rather than
   deletion.

**Search ranking with distillations:**

- Default search returns a mix; distillations get an additional
  `+α * 0.3` to importance because they save the agent context.
- Agents can request `types=[distillation]` for high-level rollups
  only, or `types=[memory, session]` for primary sources only.

**LLM choice:**

- Default: Ollama running a stronger model than the embedder (e.g.
  `llama3.2:8b` or `qwen2.5:7b`). Local-first stays consistent with
  the embedding choice, no API key required, no per-distillation cost.
- Optional: route to Claude/OpenAI via `_meta/config.yaml`. At our
  volume (a few clusters per day), API cost is cents/month and
  distillation quality is materially higher.

### Tier 3 — Hygiene worker (dedup + decay + housekeeping)

Already on ROADMAP as Phase 2 work. The worker runs on a cadence
(default nightly via cron or APScheduler in the daemon process) and
performs:

- **Pairwise dedup.** Pairs with cosine similarity ≥ 0.95 get merged.
  The higher-importance one wins; the lower one becomes a
  `deprecated_by:` redirect (still on disk, but excluded from default
  search).
- **Decay.** Importance falls over time per the curve in Tier 1.
  Memories below a threshold *and* never retrieved in 6 months drop
  out of default search (still findable with `--include-decayed`).
- **Reconciliation pass.** Re-walks all watched paths to catch files
  that changed while the daemon was offline. (Not part of v0.1 — this
  is v0.2.)
- **Bi-temporal validity.** When a fact contradicts an existing one
  (LLM-judged or explicit `supersedes:` field on a new record), the
  older record gains `valid_to: <timestamp>` rather than being
  deleted. Search defaults to "currently valid" but historical
  queries are possible via `--at <date>`.
- **Auto-skill extraction.** Detect multi-step procedures from
  session transcripts; write them as
  `skills/auto/<slug>/SKILL.md` with `provenance.source:
  hygiene-worker` and `_meta.json` describing the source span. The
  user reviews and either keeps or deletes.

## Schema additions (frontmatter)

All optional. v0.1 readers ignore them.

| Field           | Type            | Description                                                       |
|-----------------|-----------------|-------------------------------------------------------------------|
| `importance`    | float (0.0–1.0) | Score from heuristic + live signals. Unset = 0.5.                 |
| `pinned`        | bool            | True disables decay and floors `importance` at 1.0.               |
| `supersedes`    | list[id]        | This record replaces the listed older records.                    |
| `deprecated_by` | id              | This record was merged into / superseded by the named record.     |
| `valid_to`      | datetime        | Inclusive end of validity window. Default search filters this out.|
| `links`         | list[uri]       | `memory://...` URIs of related records (used by distillations).   |

The `type` enum gains one value: `distillation`.

## Search ranking pseudocode

```python
def final_score(hit, mem):
    base = hit.rrf_score
    importance = mem.frontmatter.importance or 0.5
    if mem.frontmatter.pinned:
        importance = max(importance, 1.0)
    distill_bonus = 0.3 if mem.type == "distillation" else 0.0
    return base * (1 + 0.2 * (importance + distill_bonus))
```

## Implementation phasing

The work splits cleanly into PR-sized chunks. None of them is on the
critical path for v0.1 cutover.

1. **PR-A: importance scoring (heuristic only).** Adds the
   frontmatter field, computes the seed at ingest, plugs it into
   `Search.search`. No query log, no distillations.
   *Status: shipped (Unreleased, 2026-04-28). Currently the search
   layer applies `alpha=0.2` to whatever `importance` is on a
   record's frontmatter. The pipeline doesn't yet seed
   `importance` from heuristics on ingest — that's the next slice
   of PR-A. Until then, un-annotated records get the neutral
   `0.5` default.*
2. **PR-B: query log + live boost.** Adds the `query_log` table, the
   logging hook in the MCP server, and a periodic boost step.
   *Status: query log shipped (Unreleased, 2026-04-28). Search and
   `memstem_get` write `query_log` rows tagged with the call site
   (`cli` / `mcp` / `http`). The `hygiene.query_log_enabled` config
   knob and 100k-row cap with FIFO prune are in place. The periodic
   boost step is the next slice — see PR-C.*
3. **PR-C: hygiene worker scaffold + dedup + decay.** Adds the
   nightly job, the `deprecated_by` redirect, the decay update.
   *Status: importance bump pass shipped (Unreleased, 2026-04-28).
   `memstem hygiene importance` (with `--dry-run` default and
   `--apply`) reads `query_log` and conservatively bumps importance
   on retrieved records. Idempotent via a cursor in
   `hygiene_state`. The dedup and decay slices are still pending —
   ADR 0012's pipeline picks up dedup; decay is the next slice
   here.*
4. **PR-D: session distillation.** First LLM-driven distillation
   flavor, gated behind a config flag.
5. **PR-E: topic distillation + clustering.** Second flavor; reuses
   the LLM client from PR-D.
6. **PR-F: bi-temporal validity + supersedes.** Last because it
   touches the most queries.
7. **PR-G: auto-skill extraction.** Latest because skills are
   user-facing and benefit from the prior tiers' polish.

Each PR is independently mergeable and reverts cleanly: the schema
additions are optional, the worker is gated behind config, the search
formula reduces to v0.1 behavior when `importance = 0.5` everywhere.

## Rationale

- **Frontmatter-resident state keeps the storage invariant.**
  Importance, pins, supersedes — all live in the canonical markdown
  files. Drop the index, run `memstem reindex`, every signal is
  recovered.
- **The query log is acceptably non-canonical.** It captures *user
  behavior*, not *content*. Losing it after a crash means importance
  drifts back toward heuristic-only, which is acceptable degradation.
  Backups are easy if they matter (it's a single SQLite table).
- **Distillations as records, not cache.** This is the design choice
  that took the most thought. Treating them as cache means rebuilding
  is expensive and they don't show up in normal search. Treating them
  as records means we get search, links, citations, and a clear audit
  trail for free — at the cost of having to handle staleness via
  `supersedes`/`deprecated_by`. We accept that cost.
- **Local-first LLM keeps the install footprint small.** A user who
  finished `install.sh --yes` already has Ollama. Pulling a second
  model is one command and stays inside the local-first promise. The
  hosted-API path is opt-in for users who care about quality more
  than independence.
- **Live importance boosts feel right but need a guardrail.** Naive
  retrieval-frequency boosts create runaway feedback (popular stuff
  gets more popular). The 0.2 α + 0.3 distillation bonus + 1.0 cap
  bound the effect. We can tune these post-cutover with real data.

## Consequences

**Pros:**

- Search quality improves over time without manual curation.
- Distillations give agents a "context window saver" — one paragraph
  instead of 12 source documents.
- The user gets explicit controls (`pin`, `supersedes`,
  `--include-decayed`) for cases where heuristics are wrong.
- Every signal is recoverable from the markdown vault, so the
  invariant holds.

**Cons:**

- Schema growth: 6 new optional frontmatter fields and 1 new `type`.
  Manageable, but each one is a long-term commitment once written to
  user vaults.
- The hygiene worker is the first scheduled component in Memstem.
  We'll need to decide between an in-process scheduler (APScheduler)
  and an external cron entry. The PR-C scope includes that decision.
- LLM dependency for distillations introduces variance: the same
  cluster can get summarized differently on consecutive runs. We
  accept this and rely on `deprecated_by` to redirect to the most
  recent version.
- Live importance boosts can drift toward feedback loops if the α/β
  parameters are wrong. We'll need a debug command (`memstem
  importance <id>`) to make the score breakdown legible.

## Open questions for Brad

- Is nightly the right cadence for the hygiene worker, or do you want
  it on demand (`memstem hygiene run`) until we trust it?
- For session distillation, do you want the source session to gain a
  `distilled_by:` link in its frontmatter (full bidirectional
  cross-ref) or just one-way from the distillation?
- For the LLM that writes distillations: is `qwen2.5:7b` acceptable
  as the local default, or do you want to start with a Claude/OpenAI
  call and add the local fallback later?
- Should `memstem search` default to `--include-distillations` only,
  with primary sources opt-in, or the reverse? (Current draft: mix
  by default; agents pick a filter when they care.)

These don't block writing the ADR — they're tunable knobs we can
settle as the PRs land.

## References

- ROADMAP.md → Phase 2 entries this ADR formalizes
- ARCHITECTURE.md → existing storage / search / pipeline modules
- ADR 0002 → markdown-canonical invariant this preserves
- ADR 0003 → SQLite + FTS5 + sqlite-vec choice the query log builds on
- ADR 0007 → remote ingestion, deferred until after this work lands
- PLAN.md → "Phase 2 plan — Tiered memory (v0.2)" working summary
