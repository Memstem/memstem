# ADR 0015: Recall-quality eval harness

Date: 2026-04-30
Status: Accepted

## Context

[RECALL-PLAN.md](../../RECALL-PLAN.md) lays out three buckets of work
to improve Memstem recall: finishing in-flight ADRs (importance seed,
atomic-fact extraction, retro cleanup), three search-time additions
(MMR, cross-encoder, HyDE), and one synthesis capability (reflective
synthesis). Every Bucket 2 / Bucket 3 PR lands a recall-quality
intervention whose effect is hard to assess without measurement.

Without a measurable harness, three failure modes are likely:

1. **Shipping vibes.** A change feels right ("cross-encoder rerank
   should help") but doesn't measurably improve recall. We can't tell.
2. **Regressions.** A change improves one query class but degrades
   another (a common cross-encoder failure mode). Without a per-class
   metric, the regression hides in the aggregate.
3. **Tuning paralysis.** Every PR ends with an open question — what
   λ for MMR? Which reranker model? — that we can't close without
   data.

The eval harness provides the measurement loop. It runs before each
recall-quality PR (baseline) and after (post-change), and a PR that
regresses MRR by more than 3% relative without an override is blocked
from merging.

## Decision

A YAML-driven query set + Python harness lives at
`src/memstem/eval/harness.py`. The CLI entrypoint is
`scripts/run_eval.py`. The query set lives at `eval/queries.yaml`.

### Query schema

```yaml
queries:
  - id: <unique-id>
    class: factual | conceptual | procedural | historical
    query: <natural-language query>
    expect:
      title_contains: ["substr1", ...]   # logical OR
      body_contains:  ["substr1", ...]   # logical OR
      path_contains:  ["substr1", ...]   # logical OR
    top_k: 10                             # optional; default DEFAULT_TOP_K
```

Substring matchers (case-insensitive) deliberately don't tie to a
specific `memory_id`. This is robust to dedup (the W3 retro pass will
deprecate ~20% of records) and to source migrations (a record may move
between paths). A query "found" the answer when at least one of the
top-K results matches its expect block.

### Metrics

- **MRR** — mean reciprocal rank of the first matching result, scored
  0.0 when not found in top-K.
- **Recall@3** and **Recall@10** — fraction of queries with at least
  one matching result in the top-K.
- **Per-class breakdown** — same metrics scoped to each of the four
  query classes the recall plan targets (factual / conceptual /
  procedural / historical).

### Query classes

The four classes mirror the failure modes observed in the research
that produced the recall plan:

- **factual**: "what gateway port does Ari run on" → expects an exact
  string surfaced. Today's vec retrieval struggles because
  unstructured prose contains the answer alongside other tokens that
  outrank the answer.
- **conceptual**: "memory consolidation strategy" → expects a curated
  doc surfaced. RRF + importance handles these reasonably well.
- **procedural**: "how do I send a Telegram message" → expects a skill
  or command surfaced. Today's failure mode: question and answer don't
  share vocabulary, so vec misses.
- **historical**: "what did we decide about Cloudflare" → expects a
  decision or distillation. Today's failure mode: sessions outrank
  decisions because there are more of them.

Per-class metrics let us see which classes a PR helped and which it
hurt — critical because most recall-quality techniques have asymmetric
effects.

### Logging discipline

The harness passes `log_client=None` to `Search.search`, so running
the eval doesn't write to `query_log` and doesn't bump importance on
whatever happens to surface. The eval reads live state; it shouldn't
change it. (A future "eval-driven training" loop could opt into
logging — that's deliberate scope, not the default.)

### Where it runs

- **Locally**: `scripts/run_eval.py --vault ~/memstem-vault` prints a
  human-readable report.
- **CI**: every PR that touches `core/search.py`, `core/rerank.py`
  (when it lands), `hygiene/importance.py`, or `core/extraction.py`
  runs the eval and posts the diff vs the base branch. PRs that
  regress MRR by > 3% relative are blocked without human override.
- **Self-test**: `tests/test_eval_harness.py` runs the harness against
  a tmp_path-built fixture vault with five engineered memories and
  three engineered queries. This validates the harness logic itself
  without depending on the live vault.

## Schema additions

None to canonical markdown. Eval queries live outside the vault, in
the repo, version-controlled with the code.

## Implementation phasing

One PR. Layout:

| File                                       | Purpose                                  |
|--------------------------------------------|------------------------------------------|
| `src/memstem/eval/__init__.py`             | Public re-exports                        |
| `src/memstem/eval/harness.py`              | Loaders, scoring, reporting              |
| `eval/queries.yaml`                        | Hand-curated production query set        |
| `scripts/run_eval.py`                      | CLI entrypoint                           |
| `tests/test_eval_harness.py`               | Self-tests using a tmp_path fixture vault|
| `docs/decisions/0015-eval-harness.md`      | This ADR                                 |

The query set ships as a starter (12 queries across 4 classes). It
grows as new failure modes surface — Brad adds queries; Claude Code
reviews; both can approve. The contract is "real failure modes from
real recall problems" — synthetic queries are forbidden because they
optimize the harness, not the system.

## Rationale

- **YAML over JSON** for the query set because hand-curated content
  needs to be readable and commentable.
- **Substring matchers over exact memory_ids** because IDs change on
  retro-cleanup and queries shouldn't break.
- **Per-class metrics over a single MRR** because most techniques in
  Bucket 2/3 have asymmetric effects across query classes; we need to
  see them.
- **No live `query_log` write** because the eval is a measurement,
  not a usage event. Importance signal should reflect what users do,
  not what the eval does.
- **Self-test fixture in tmp_path** rather than committed fixture
  files so we don't carry test-vault binaries; the test builds five
  memories programmatically and tears them down.

## Consequences

**Pros:**

- Every recall-quality PR has a measurable bar to clear.
- Per-class breakdown surfaces asymmetric effects before merge.
- The query set is itself a working agenda — failed queries are the
  next things to fix.
- Cheap to run (under 60s on Brad's box).

**Cons:**

- The query set is biased toward known failure modes. Mitigation:
  expand quarterly as new ones surface.
- The harness doesn't measure precision (false positives in top-K).
  Acceptable for v1 — recall is the dominant problem; precision
  becomes interesting after we cap recall.
- Substring matchers are forgiving — a query that surfaces a wrong
  record containing the substring still scores. Mitigation: substrings
  are chosen specifically (e.g., "18789" not "port"); false matches
  are rare in practice.

## Open questions

- Should the eval gate on absolute MRR thresholds (e.g., "MRR must be
  >= 0.6 to merge") or only on relative regression (>3% drop)?
  Current draft: relative-regression only — absolute thresholds
  would block early PRs from a low baseline.
- Should we introduce a "negative test" class — queries that *should
  not* return certain records? Useful for measuring precision.
  Deferred to a follow-up ADR if it becomes necessary.

## References

- [RECALL-PLAN.md](../../RECALL-PLAN.md) — overall agenda
- ADR 0008 ([decisions/0008](./0008-tiered-memory.md)) — importance
  scoring this eval gates
- ADR 0011 ([decisions/0011](./0011-noise-filter-and-fact-extraction.md))
  — extraction this eval gates
- ADR 0012 ([decisions/0012](./0012-llm-judge-dedup.md)) — dedup
  this eval gates
