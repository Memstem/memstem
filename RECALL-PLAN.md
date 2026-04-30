# Memstem Recall Quality — Plan

> Created 2026-04-29; simplified 2026-04-30.
> Companion to [PLAN.md](./PLAN.md) (Phase 1 cutover). This is the
> Phase 2+ recall-quality agenda. Approved by Brad 2026-04-30.

## TL;DR

Live diagnostics show three concrete recall failures (~20% duplicate
records, importance signal inert, sessions outranking curated answers).
Memstem already has 8 of the 11 cleanup mechanisms we need; the work
here is *finishing* the unshipped halves and adding 4 search-time /
synthesis lifts. Eval harness gates everything — no quality PR ships
without measurable improvement.

## §1 — What's already in Memstem (don't duplicate)

| Surface | Where | Status |
|---|---|---|
| Noise filter at ingest | `core/extraction.py` | Shipped |
| Body-hash dedup at ingest (Layer 1) | `core/dedup.py` + `core/pipeline.py` | Shipped |
| Vec-similarity dedup *candidates* | `hygiene/dedup_candidates.py` | Shipped (read-only) |
| LLM dedup *judge* | `hygiene/dedup_judge.py` | Shipped (NoOp default) |
| Importance multiplier in search | `core/search.py` | Shipped |
| Importance bumps from retrievals | `hygiene/importance.py` | Shipped |
| Distillation *candidates* | `hygiene/distillation.py` | Shipped (read-only) |
| Bi-temporal `valid_to` filter | `core/search.py` | Shipped |
| Importance seed at ingest | (ADR 0008 PR-A) | **NOT shipped** |
| Atomic-fact extraction | (ADR 0011 PR-D) | **NOT shipped** |
| Decay over time | (ADR 0008 Tier 3) | **Partly shipped** |
| Dedup *resolution* (apply judge verdicts) | (ADR 0012 PR-D) | **NOT shipped** |

## §2 — Live diagnostics (audit run 2026-04-29)

Source: `/tmp/audit_dedup_retro.py` against
`~/memstem-vault/_meta/index.db`. Full JSON at
`/tmp/memstem-dedup-retro-audit.json`.

- 1,229 indexed memories.
- **240 body-hash collision groups, 245 deprecate candidates** (~19.9%
  of vault). Mostly size-2 pairs; 2 size-4 (SEO skills);
  244/245 from openclaw source.
- **8 skill-involved groups** → review queue per ADR 0012.
- **211 of 240 groups (88%) are coin flips** because importance is
  null, retrievals are 0 → only timestamp tiebreak distinguishes
  winners. *This is why W1 (importance seed) ships before W3 (retro
  cleanup).*
- **Cron-titled noise** is a separate problem: 149+ "Fleet Task
  Monitor" records have unique bodies (each cron run is different),
  so byte-hash dedup doesn't catch them. They're noise by *category*,
  not by *exact match*.

## §3 — Work items

Three buckets, 8 items total (W0–W7). Each is one PR.

### Bucket 1 — Finish what's already approved

No new ADR needed. Existing ADRs cover the design.

#### W0 — Eval harness (gates every Bucket 2/3 PR)

Goal: 30 hand-crafted queries with known-correct answers + a harness
that prints MRR + top-3/top-10 recall, before/after each PR.

- `tests/eval/queries.yaml` — 30 queries (8 factual, 8 conceptual,
  8 procedural, 6 historical), each with `expected_memory_ids` or
  `expected_titles`.
- `scripts/run_eval.py` — runs queries through `Search.search`,
  computes per-class + aggregate metrics, JSON dump for diffing.
- Pytest integration: `tests/test_eval_harness.py` self-tests the
  harness against a fixture vault.
- Acceptance: harness completes < 60s on Brad's box; per-class
  metrics reported; clean failure mode when vault empty.

LOC: ~250. Risk: low. ADR: new (0015-eval-harness, ~80 lines).

#### W1 — Importance seed at ingest (finishes ADR 0008 PR-A)

Goal: pipeline computes a heuristic importance at ingest so the
shipped search-side multiplier actually has signal to amplify.

- `core/importance_seed.py` — pure `compute_seed(record,
  inbound_link_count) -> float`. Signals per ADR 0008 Tier 1: type
  weight, recency, wikilink density, length penalty.
- Pipeline wiring: compute seed before `vault.write`, set on
  frontmatter.
- `memstem reindex --reseed-importance` flag — one-shot backfill on
  the existing 1,229 records.
- Acceptance: top 10 by importance after reseed are skills + decisions
  + curated MEMORY.md (sanity check); search ranking visibly differs
  from RRF order on a fixture (eval harness verifies).

LOC: ~250. Risk: low (config-driven weights; eval gates regressions).
ADR: existing ADR 0008 Tier 1.

#### W3 — Retro cleanup pass (one CLI, two retro sweeps)

Goal: apply *already-shipped* Layer 1 dedup + noise filter to records
that pre-date those PRs. One CLI command for both.

- New `memstem hygiene cleanup-retro` with `--dry-run` (default) and
  `--apply`. Subcommands or flags: `--dedup`, `--noise`, `--all`.
- Module `hygiene/cleanup_retro.py`:
  - Body-hash collision finder + winner selection (importance →
    retrievals → updated → id, matching the audit script).
  - Noise-rule replay against existing records (drops set
    `valid_to=now`, soft-delete; never hard-delete).
  - Skill collisions route to `vault/skills/_review/` (NEVER
    auto-merged per ADR 0012).
  - Idempotent: re-runs on a clean vault are no-ops.
- Acceptance: post-`--apply`, audit script reports zero collision
  groups; default search excludes deprecated records; skill review
  tickets exist for the 8 skill-involved groups; re-running is a no-op.

LOC: ~400. Risk: medium (mutating real vault frontmatter — but every
action is reversible because everything is markdown edit). ADR:
addendum to ADR 0012 (~40 lines).

### Bucket 2 — Three search-time additions (need new ADRs)

#### W4 — MMR diversification

Goal: top-K stops being filled with paraphrases; top results cover
different angles of the query.

- `core/mmr.py` with `mmr_rerank(candidates, query_emb, lambda=0.7,
  k=10)`. Standard formula: `λ * sim(q,c) - (1-λ) * max(sim(c,picked))`.
- Wires into `Search.search` as the post-RRF rerank step. Config
  knob `ranking.mmr.{enabled, lambda, k}`. Default-off until eval
  passes.
- Acceptance: eval harness shows ≥10% drop in pairwise within-top-5
  cosine; no MRR regression.

LOC: ~150. Risk: low. ADR: new (0016-mmr-diversification).

#### W5 — Cross-encoder rerank

Goal: biggest documented precision lift; standard production-RAG step
we don't have.

- `core/rerank.py` with `Reranker` ABC, `OllamaReranker` (default,
  `bge-reranker-base` configurable), `NoOpReranker`.
- Top-50 RRF candidates re-scored by cross-encoder, re-sorted before
  truncation. Cached on `(query, memory_id, body_hash)`.
- Acceptance: eval harness shows ≥15% MRR improvement; p95 latency
  < 500ms.

LOC: ~300. Risk: medium (model selection + tuning loop). ADR: new
(0017-cross-encoder-rerank).

#### W6 — HyDE query expansion

Goal: fix "how do I X" queries that fail because question and answer
don't share vocabulary.

- `core/hyde.py` with `expand_query(query, llm) -> str` and
  `should_expand(query) -> bool` (gates out short / exact-string queries).
- Uses existing Ollama client. Per-query cache on the hypothetical
  answer.
- Acceptance: ≥20% MRR improvement on procedural-class queries; no
  factual-class regression.

LOC: ~250. Risk: medium (LLM latency). ADR: new (0018-hyde).

### Bucket 3 — One genuinely new capability (needs new ADR)

#### W7 — Reflective synthesis (REM-phase equivalent)

Goal: the only Dreaming capability not on Memstem's roadmap. Weekly
LLM pass writes cross-cluster reflection records.

- `hygiene/reflection.py`. CLI `memstem hygiene reflect`.
- Reads top-100 highest-importance memories, prompts LLM for 3-5
  reflective patterns with citations to source records.
- Each pattern → `type: reflection` record with `links`.
- Citations required (≥3 source ids); reflections without valid
  cites are rejected.
- Older reflections gain `deprecated_by` when newer ones cover the
  same pattern.
- Acceptance: queryable reflection records; dedup against prior
  reflections via Layer 3 judge.

LOC: ~400. Risk: medium-high (hallucination risk; mitigated by
citation requirement). ADR: new (0019-reflective-synthesis).

#### W2 — Atomic-fact extraction (ADR 0011 PR-D, ready as written)

Ships ADR 0011 PR-D as designed. Sessions get split into atomic facts
at ingest; original preserved as `session_raw`.

LOC: ~600 (largest single PR). ADR: existing ADR 0011 covers the
design.

## §4 — Order of execution

```
Block 1 (this session, if scope allows):
  1. W0 eval harness     (gates everything)
  2. W1 importance seed  (must precede W3)
  3. W3 retro cleanup    (depends on W1)
  4. W4 MMR              (independent; quick win)

Block 2 (next session — needs ADR + tuning):
  5. W5 cross-encoder
  6. W6 HyDE

Block 3 (later — bigger ships):
  7. W2 atomic facts
  8. W7 reflections
```

If a 4-week ship is needed: do W0 + W1 + W3 + W5 only. That captures
most of the precision lift; W7 ships in v0.3 instead.

## §5 — Dependencies

- W0 has no deps. Ship first.
- W1 has no deps. Ship next.
- W3 depends on W1 (winner selection needs real importance signal).
- W4/W5/W6 are independent of each other; each is independent of
  W1/W3.
- W7 depends on W1 (importance seed picks the top-100) and benefits
  from W2 (atomic facts make better reflection input).
- W2 has no hard deps; benefits from W1 for fact-importance seeding.

## §6 — What was deferred (don't do these yet)

- **Type-aware decay floors** — small enough to fold into the
  in-flight decay PR; not its own work item.
- **Negative-feedback signal** — same; one config knob in the
  importance pipeline when we get there.
- **Query-class routing** — defer until we measure that W4+W5+W6
  aren't enough on their own. Don't ship complexity on speculation.
- **High-recall promotion** — overlap with distillation; defer or
  fold into W7 reflections later.

## §7 — Risks + mitigations

| Risk | Mitigation |
|---|---|
| Retro pass picks wrong winner | Audit dry-run + skill review queue + `coin_flip` flag in plan |
| Importance weights bias retrieval | Eval harness gates merge; weights are config |
| LLM unreliability (W2/W6/W7) | NoOp fallbacks; eval gates `enabled: true` flips |
| Reflection hallucinates patterns | ≥3 citation requirement; reject reflections without valid cites |
| Schema additions break old readers | All new fields optional; v0.1 readers ignore unknowns |

## §8 — Open questions Brad will resolve as PRs land

1. Reranker model: `bge-reranker-base` (smaller, faster) vs
   `mxbai-rerank-large` (bigger, slower, higher quality)? Decide
   in W5 with eval data.
2. Reflection cadence: weekly (default) or daily?
3. Skill-collision policy in W3: ship with minimal `_review/`
   writer or block on ADR 0012 PR-E (skill-review CLI)?

These don't block writing the plan or starting work — tunable knobs.

## §9 — References

- [PLAN.md](./PLAN.md) — Phase 1 cutover
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design
- ADR 0008 ([decisions/0008](./docs/decisions/0008-tiered-memory.md))
- ADR 0011 ([decisions/0011](./docs/decisions/0011-noise-filter-and-fact-extraction.md))
- ADR 0012 ([decisions/0012](./docs/decisions/0012-llm-judge-dedup.md))
- `/tmp/audit_dedup_retro.py` + `/tmp/memstem-dedup-retro-audit.json`
  — read-only retro-dedup audit (run 2026-04-29)
