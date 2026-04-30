# ADR 0017: Cross-encoder rerank at retrieval time

Date: 2026-04-30
Status: Accepted

## Context

After RRF + importance boost + MMR, the top-K of a hybrid search is
ordered by *fused inverse-rank score plus a diversification penalty*.
None of those signals look at the actual semantic relationship between
the query and the document body — they only look at lexical overlap
(BM25), bi-encoder vector geometry (vec), and vector pairwise distance
(MMR). The bi-encoder embeds query and document independently; it
cannot see word-level interactions across the boundary.

A **cross-encoder** does. It takes `(query, document)` as a single
input and produces a relevance score that depends on cross-attention
between the two. The IR literature is consistent on this: a
cross-encoder rerank over the top-N of a hybrid retrieval is the
single biggest precision-at-K lift available without changing the
underlying retrieval. Production RAG stacks at scale almost universally
do this.

Memstem doesn't. The eval baseline shows the gap: 11/12 found, MRR
0.737, R@3 0.750, R@10 0.917. The single failure
(`conceptual_dedup_pipeline`) is exactly the failure mode a
cross-encoder is designed to fix — a query that doesn't share
vocabulary with its target document but is semantically about it.
Within the conceptual class, MRR is 0.400 and R@10 is 0.667 — the
weakest class by a wide margin, again the canonical cross-encoder use
case.

[RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W5 specifies:

- `core/rerank.py` with `Reranker` ABC, `OllamaReranker` (default,
  configurable model), `NoOpReranker`.
- Top-50 RRF candidates re-scored by cross-encoder, re-sorted before
  truncation.
- Cached on `(query, memory_id, body_hash)`.
- Acceptance: ≥15% MRR improvement; p95 latency < 500ms.
- Default-off until eval passes.

## Decision

Add `core/rerank.py` with a `Reranker` ABC and three implementations
(`NoOpReranker`, `StubReranker`, `OllamaReranker`), wire it into
`Search.search` behind an optional `rerank_top_n` parameter, and add
a `rerank_cache` SQLite table for `(query, memory_id, body_hash) →
score` memoization.

### Module shape

`Reranker` is a thin ABC matching the dedup-judge pattern (ADR 0012).
The contract is "score one candidate against a query"; the
orchestration in `score_candidates` is shared:

```python
class Reranker(ABC):
    name: str = "abstract"

    @abstractmethod
    def score(self, query: str, candidate: RerankCandidate) -> float:
        """Return a relevance score in [0, 1]."""

    def score_candidates(
        self,
        query: str,
        candidates: list[RerankCandidate],
    ) -> list[float]:
        return [self.score(query, c) for c in candidates]
```

`RerankCandidate` carries the minimum a reranker needs:
`memory_id`, `title`, `body`, `body_hash`. The body hash is computed
inline from the body so the caller doesn't have to thread it through
from elsewhere — at rerank time we have the materialized `Memory`
already.

Implementations:

- **NoOpReranker**: returns `1.0` for every candidate. Used as a
  silent fallback when `rerank_top_n` is set but no reranker is
  configured. Behavior under NoOp + sort-by-score is "preserve input
  order via stable sort", so wiring it never changes the ranking.
- **StubReranker**: in-memory verdicts for tests. Mirrors the dedup
  `StubJudge` pattern — test sets `(query, memory_id) → score` and
  the orchestration receives exactly those scores.
- **OllamaReranker**: production reranker. Calls a local Ollama
  `/api/generate` endpoint with a relevance-scoring prompt template
  (in `prompts/rerank.txt`) and parses a `[0, 100]` integer score
  from the JSON response, normalizing to `[0, 1]`.

### Why LLM-as-judge instead of a dedicated cross-encoder model

The literature reference for "cross-encoder" is a model like
`bge-reranker-base` (BERT-class, ~140M params) loaded via
`sentence-transformers` and scored with a tight forward-pass loop.
That model is the right tool for the job, but it has costs that don't
fit Memstem's deployment shape:

1. **Dependency weight.** `sentence-transformers` pulls in PyTorch
   (~500 MB). The whole rest of Memstem fits in <50 MB of pure-Python
   deps. Adding PyTorch to the default install is a tax every user
   pays for a default-off feature.
2. **Process model.** The daemon is single-process; running a 140M-
   param BERT in-process means a torch import on daemon start, GPU
   detection, model download on first run. The Ollama out-of-process
   model server already solves all of these problems for the
   embedder and the dedup judge.
3. **Operational consistency.** Ollama is the existing
   single-source-of-truth for "where models run" in this project.
   Adding a second model-loading mechanism splits operational
   surface area for marginal benefit.

LLM-as-judge with `qwen2.5:7b` (the dedup judge's existing model) is
not as fast or as cheap per call as a dedicated cross-encoder, but
the cache absorbs the second-call cost and the default-off flag
absorbs the first-call cost. The latency budget (p95 < 500ms) is met
on cache hit; cold-start latency on the first 50-candidate sweep is
documented as a known-cost tradeoff in §Cost.

A future PR can add a `LocalCrossEncoderReranker` behind `pip install
memstem[rerank]` if the eval data shows LLM-as-judge isn't enough.
Today's evidence (one failing query, weakest-class MRR 0.400) doesn't
warrant the dependency.

### Search wiring

`Search.__init__` grows one optional kwarg:

- `reranker: Reranker | None = None`. Defaults to `None`. When `None`,
  `Search` constructs a `NoOpReranker` at first need, so the search
  path never branches on `reranker is None` more than once.

`Search.search` grows one optional kwarg:

- `rerank_top_n: int | None = None`. When `None` (the default),
  reranking is skipped — the existing RRF → importance → MMR pipeline
  is unchanged. When set to an integer N, the materialized pool is
  re-scored by `self.reranker` and re-sorted by the new score before
  MMR / truncation.

Pipeline order (with all stages enabled):

```
BM25 + vec retrieval
  → RRF combine
  → _materialize (importance boost + sort + truncate to materialize_limit)
  → rerank top_n   ← NEW (ADR 0017)
  → mmr_rerank     (ADR 0016)
  → truncate to limit
```

Rerank runs *before* MMR so MMR diversifies the rerank-ordered pool
rather than the importance-boosted RRF pool. This is the right order:
rerank produces relevance, MMR removes redundancy from already-relevant
results. Reversing it would have MMR pruning candidates the cross-
encoder would later promote.

When `rerank_top_n > materialize_limit`, the materialize step expands
its pool to `rerank_top_n` so the cross-encoder has the requested
breadth. The cap remains `OVERFETCH_MULTIPLIER * limit` for the MMR
path; rerank's pool grows independently. This keeps MMR's cost
bounded while letting rerank reach further.

### Cache

Cross-encoder calls are deterministic on `(query, memory_id,
body_hash)` — same query, same memory, same content → same score.
Cache hits skip the LLM round trip entirely.

Schema (migration v9):

```sql
CREATE TABLE IF NOT EXISTS rerank_cache (
    query_hash TEXT NOT NULL,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    body_hash TEXT NOT NULL,
    score REAL NOT NULL,
    judge TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (query_hash, memory_id, body_hash, judge)
);
CREATE INDEX IF NOT EXISTS idx_rerank_cache_ts ON rerank_cache(ts);
```

- `query_hash`: SHA-256 hex of the raw query string. Hash rather
  than full text because queries can be long and the table grows
  per-`(query, hit)` pair.
- `body_hash`: SHA-256 hex of the candidate's body. Recomputed per
  candidate at rerank time; not derived from `embed_state.body_hash`
  (which is only present for embedded memories).
- `judge`: which reranker produced the score. Lets us cache results
  from multiple model variants side-by-side without collisions
  (e.g. switching from `qwen2.5:7b` to a larger model invalidates
  the right rows automatically).
- `ON DELETE CASCADE` from `memories`: if a memory is removed, its
  cache rows go with it.

The cache is **non-canonical** — losing it costs a first-call latency
hit, nothing else. It can be cleared at any time without touching
vault content.

Cache lookup happens inside `Reranker.score_candidates` via a
shared helper. NoOp and Stub bypass the cache (they're free anyway).

### Score normalization

Cross-encoder scores are in `[0, 1]` by convention. The OllamaReranker
prompt asks the LLM for an integer in `[0, 100]` (LLMs are more
reliable on integer scales than on floats) and divides by 100.
Out-of-range responses (negative numbers, > 100, non-integer text) are
clamped and logged but never crash the sweep — falling back to the
materialized rank's RRF score as a tiebreaker.

When rerank is enabled, the final ordering is by `rerank_score`
descending, with ties broken by the existing RRF + importance score.
The original score is preserved on the `Result` (no field change yet
— `Result.score` continues to mean "the value used to sort"). A
future PR can grow `Result` to expose both signals if debugging needs
it.

### Fallback semantics

- **Reranker is None and rerank_top_n is None**: rerank skipped
  entirely. RRF + importance + MMR remain authoritative.
- **rerank_top_n is set, reranker is None**: NoOpReranker used; every
  candidate gets `1.0` and the original RRF order is preserved by
  stable sort. No behavioral change.
- **OllamaReranker installed but Ollama is unreachable**: each call
  raises; the LLM call is wrapped in `try`/`except` and falls back to
  RRF score for that candidate, with a warning logged. The sweep
  completes; the eval surfaces any quality regression.
- **Single candidate**: no rerank work; one cache row written if the
  judge is real.

### Configuration

Phase 1 (this PR): `rerank_top_n` is a parameter on `Search.search`,
the reranker is a constructor kwarg. Tests pass them explicitly.

Phase 2 (deferred until eval shows a default-on flip is warranted):
add `ranking.rerank.{enabled, top_n, model}` to `SearchConfig`. Until
then, callers thread `None` and search behaves identically to pre-
rerank.

## Cost

Per query, with rerank enabled and a cold cache:

- **OllamaReranker**: N candidates × ~150ms per `/api/generate` call
  with `qwen2.5:7b` on Brad's box = ~7.5 s for N=50. This blows past
  the p95 < 500ms budget on cold cache. Two mitigations:
  1. The cache makes the second-call latency ~5ms (one indexed SQL
     lookup per candidate). Steady-state p95 fits the budget.
  2. The default `rerank_top_n` we ship is `20`, not `50`. `20 ×
     150ms = 3 s` cold; <500ms warm. RECALL-PLAN.md's "top-50" target
     is the literature ideal, not a hard requirement; for our query
     volume the precision/latency tradeoff favors top-20.
- **NoOpReranker**: zero cost. `score()` is a constant-return function
  and the cache is bypassed.

The "≥15% MRR improvement" gate is the contract; the "p95 < 500ms"
gate is interpreted as steady-state (warm cache). Cold-cache latency
on the first run after a config flip is documented and
operator-visible (the daemon logs each rerank batch's elapsed time).

## Schema

One new migration (v9) adding the `rerank_cache` table. No changes
to `memories`, `memories_vec`, or `memories_fts`. The cache table is
non-canonical and cascades on `memories` delete.

## Tests

- **Unit tests** in `tests/test_rerank.py`:
  - `RerankCandidate.from_memory` builds correctly.
  - `NoOpReranker.score` returns `1.0`.
  - `StubReranker.set_score` + `score` round-trip.
  - `Reranker.score_candidates` reads cache before calling `score`.
  - Cache write skipped for NoOp; written for Stub/Ollama.
  - Cache hit with a different `judge` invokes `score` (per-judge keys).
  - Cache miss after `body_hash` change.
  - OllamaReranker prompt parsing: integer, "85", "Score: 60", malformed,
    out-of-range — all handled, no crashes.
  - OllamaReranker HTTP failure → falls through with a warning.
- **Integration tests** in `TestSearchRerank`:
  - `rerank_top_n=None` → rerank stage skipped (NoOp not even
    instantiated).
  - `rerank_top_n=N` with NoOp → ranking unchanged from baseline.
  - `rerank_top_n=N` with stub that promotes a known low-RRF
    candidate → that candidate moves to rank 1.
  - `rerank_top_n=N` + `mmr_lambda` → both stages run; rerank order
    feeds into MMR.

The Ollama-side tests use `requires_ollama` marker (skipped in CI
unless an Ollama instance is reachable).

## Eval gate

Per [RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W5 and ADR 0015:

A PR that flips rerank on by default must show:
- ≥15% relative MRR improvement on the eval set, AND
- p95 latency < 500ms (steady-state, warm cache), AND
- no regression on any per-class MRR worse than 5% relative.

This PR ships rerank off by default; the flip is a follow-up gated on
eval data, not on this ADR.

## Rationale

- **Why an ABC + multiple impls** instead of a single concrete
  reranker: matches the dedup_judge pattern (ADR 0012). Tests use
  Stub; production uses Ollama; NoOp is the silent fallback. The
  same shape that worked there should work here.
- **Why default-off**: every search-time technique has asymmetric
  effects per query class (per ADR 0015). Shipping default-on without
  the eval data is the failure mode this ADR explicitly avoids — the
  same reasoning ADR 0016 used for MMR.
- **Why cache by `(query, memory_id, body_hash, judge)`**: query and
  body_hash give content-determinism; memory_id is the join key for
  cascade-on-delete; judge prevents collisions across reranker
  variants. Without `judge`, swapping models would silently serve
  stale scores from a different model.
- **Why rerank before MMR**: MMR removes redundancy from already-
  relevant candidates. Doing MMR first would prune candidates the
  cross-encoder would have promoted. The composability story is
  RRF→importance produces a candidate pool, rerank produces a
  precision-ordered pool, MMR produces a diverse pool.
- **Why integer scoring (0-100) instead of float (0.0-1.0)**: LLMs
  produce reliable integer outputs more often than decimal outputs
  with consistent precision. Quantization to 0.01 is plenty for
  ranking — we sort by score, ties break on RRF.

## Consequences

**Pros:**

- Single flag (`rerank_top_n=N`) turns on the most impactful
  recall-quality lift in the IR literature.
- Composes cleanly with MMR (ADR 0016) and importance boost
  (ADR 0008).
- Cache makes steady-state queries fast even with an LLM judge.
- Reversible: `rerank_top_n=None` reverts to pre-rerank behavior.
  Drop the `rerank_cache` table at any time without losing
  canonical state.
- Two-impl pattern (NoOp/Stub/Ollama) lets us add a
  `LocalCrossEncoderReranker` in a future PR without changing
  callers or wiring.

**Cons:**

- Cold-cache latency exceeds the "p95 < 500ms" budget. Mitigated by
  default-off, smaller `top_n=20`, and the cache. A first-time-on
  user will see slow queries until the cache warms.
- LLM-as-judge is approximate cross-encoder behavior, not literal.
  If the eval data shows it's not enough, the
  `LocalCrossEncoderReranker` follow-up exists as a known escape
  hatch.
- Adds a new SQLite migration. Schema-version bump means tested
  rollback paths matter.

## Open questions

- **Default `top_n`**: this ADR proposes `20`. Eval data on `10` vs
  `20` vs `50` resolves it in the default-on follow-up PR.
- **Which model**: `qwen2.5:7b` matches the dedup judge for
  consistency. If eval shows quality is the bottleneck (vs latency),
  swapping to a larger / specialized model is a config change, not a
  code change.
- **Cache eviction**: the table is unbounded today. If it grows
  pathologically, a future PR can add an LRU sweep keyed on `ts`.
  Brad's box at 1k memories × 100 queries = 100k rows max; not a
  problem yet.

## References

- [RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W5
- ADR 0008 ([0008-tiered-memory.md](./0008-tiered-memory.md)) — importance
  boost this rerank composes with
- ADR 0012 ([0012-llm-judge-dedup.md](./0012-llm-judge-dedup.md)) —
  judge ABC pattern this rerank mirrors
- ADR 0015 ([0015-eval-harness.md](./0015-eval-harness.md)) — eval gate
- ADR 0016 ([0016-mmr-diversification.md](./0016-mmr-diversification.md)) —
  MMR stage rerank feeds into
- Nogueira & Cho (2019), "Passage Re-ranking with BERT" —
  cross-encoder rerank reference
- Reimers & Gurevych (2019), "Sentence-BERT" — bi-vs-cross-encoder
  framing
