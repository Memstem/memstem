# ADR 0016: MMR diversification at retrieval time

Date: 2026-04-30
Status: Accepted

## Context

After RRF + importance boost, the top-K of a hybrid search can still be
dominated by paraphrases or near-duplicates of the same fact. Three
distinct sources contribute:

1. **Pre-W3-cleanup duplicate damage.** ~20% of Brad's vault is
   byte-hash duplicates (per the 2026-04-29 audit). Even after W3
   ships and the retro pass runs, new duplicate ingestion can race
   ahead of the dedup hash check, and "near-duplicates" (paraphrases,
   different timestamps, formatting variations) won't be caught by
   Layer 1 at all — that's Layer 3's job and Layer 3 runs in the
   hygiene worker, not at search time.

2. **Multiple records about the same topic.** Brad has 50+ daily logs
   that mention "Cloudflare migration" across different days. They're
   all individually valid memories — none are duplicates — but a
   query for "Cloudflare migration" surfaces 5+ near-paraphrases of
   the same conclusion in the top-10, pushing genuinely new angles
   off the page.

3. **Embedding chunk overlap.** Long documents are chunked at ingest;
   each chunk gets its own vector. Two chunks of the same source
   document sometimes both rank in the top-K, creating apparent
   duplicates that are actually the same document twice.

ADR 0008 / 0011 / 0012 don't address this. RRF is a fusion strategy,
not a diversification strategy. Importance is a relevance modifier,
not a redundancy penalty. The retro cleanup pass (W3) collapses
exact body-hash matches but doesn't touch paraphrases.

The IR literature has a standard answer: **Maximal Marginal Relevance
(MMR)**. After producing a top-N candidate set, re-rank greedily so
each pick balances *relevance to the query* against *redundancy with
the picks already made*. The formula is:

```
mmr_score(c) = λ * sim(c, query) - (1 - λ) * max(sim(c, picked))
```

with `λ ∈ [0, 1]`. Literature default is 0.7 — diverse enough to
break duplicate streaks, not so diverse that high-relevance results
get demoted aggressively.

## Decision

Add `core/mmr.py` with a pure `mmr_rerank` function and wire it into
`Search.search` behind an optional `mmr_lambda` parameter. The default
is `None` (MMR disabled, RRF + importance ordering is final) until the
eval harness (W0) shows a measurable improvement, at which point we
flip the default in `SearchConfig`.

### Function shape

```python
def mmr_rerank(
    candidates: list[T],
    query_embedding: Sequence[float],
    embedding_lookup: Callable[[T], list[float] | None],
    *,
    lambda_: float = DEFAULT_MMR_LAMBDA,  # 0.7
    k: int = DEFAULT_MMR_K,                # 10
) -> list[T]:
```

The module is generic over `T` so it doesn't import from `core.search`
(avoiding the circular import). Callers pass any item type and a
lookup callable that knows how to fetch its embedding. `Search` passes
in `Result` and a closure that pulls the first chunk's embedding from
`memories_vec`.

### Search wiring

`Search.search` grows one parameter:

- `mmr_lambda: float | None = None`. When `None`, MMR is disabled and
  search behaves exactly as before. When set, MMR runs over a wider
  pool (up to `OVERFETCH_MULTIPLIER * limit`) and returns the top-K.

Helper: `Search._first_chunk_embedding(memory_id)` reads the first
chunk's embedding from `memories_vec`. Returns `None` when the memory
has no vectors (embedder not yet drained, or BM25-only setup).
No-embedding candidates are appended to the end of the MMR-ordered
list at original RRF rank.

### Fallback semantics

MMR requires a query embedding. When BM25-only retrieval is in use
(no embedder configured, or embedder failed), MMR is silently skipped
and the result reduces to RRF + importance ordering. This is
intentional — `mmr_lambda` is a tunable, not a contract.

### Lambda clamping

Out-of-range `lambda_` values are clamped to `[0.0, 1.0]` silently.
The most common source is misconfigured YAML (`mmr_lambda: 1.5`); we
prefer "do something reasonable" over "raise". The clamp is logged
nowhere — observability of bad config is the eval harness's job
(metrics drop), not a runtime warning.

## Configuration

Phase 1: parameter on `Search.search`. Tests pass it explicitly.

Phase 2 (deferred until eval harness shows a default-on flip is
warranted): add `ranking.mmr.{enabled, lambda, k}` to `SearchConfig`.
Until then, every caller threads `None` and the search behaves
identically to pre-MMR.

## Cost

MMR adds one cosine computation per `(picked, candidate)` pair —
bounded by `limit * limit` for top-N candidates against `limit` picks.
For typical `limit=10` queries this is 100 cosine ops; cosine on a
768-dim vector is microseconds in Python. Total overhead per query is
negligible (~1ms on Brad's box).

The `_first_chunk_embedding` lookup is one SQL row per candidate —
also bounded and indexed. `OVERFETCH_MULTIPLIER * limit = 50` lookups
per query in the typical case.

## Schema

None. No frontmatter changes, no index changes.

## Tests

- 15 unit tests in `tests/test_mmr.py` covering edge cases (empty
  candidates, zero k, no query embedding, no embeddings, λ clamping)
  and behavior (λ=1 preserves order, λ=0 promotes diversity, default
  λ demotes paraphrases when geometry permits).
- 3 search-integration tests in `TestSearchMmr` verifying disabled /
  enabled / fallback paths through `Search.search`.

## Eval gate

Per RECALL-PLAN.md §5: a PR that flips MMR on by default must show
≥10% reduction in pairwise within-top-5 cosine on the eval set with
no MRR regression. This PR ships MMR off by default; the flip is a
follow-up gated on eval data, not on this ADR.

## Rationale

- **Why generic `T`** rather than typing on `Result`: prevents an
  import cycle between `core.mmr` and `core.search`. The module is
  conceptually generic anyway; coupling it to `Result` would only
  make refactors harder.
- **Why default-off**: every search-time technique has asymmetric
  effects per query class (per ADR 0015). Shipping default-on without
  the eval data is the failure mode this ADR explicitly avoids.
- **Why first-chunk embeddings only**: long documents are chunked.
  Pairwise similarity over all chunks would be O(chunks²) and the
  marginal information gain is small. The first chunk is a sufficient
  proxy for "what is this document about" — same choice the
  dedup-candidate generator made.
- **Why λ=0.7 default**: literature consensus across multiple IR
  surveys (BERT-rerank papers, RAG textbooks). Some work prefers 0.5
  for question answering; 0.7 is the safer choice for general search.

## Consequences

**Pros:**

- One option flag turns on diversification across every query — no
  per-query tuning.
- Composes cleanly with the existing pipeline: RRF → importance →
  MMR → truncate. Each step is a separable concern.
- Cheap. ~1ms per query overhead.
- Reversible. Setting `mmr_lambda=None` reverts to pre-MMR behavior.

**Cons:**

- λ=0.7 isn't right for every query class. Eval harness will surface
  asymmetries; query-class routing (deferred per RECALL-PLAN.md) can
  pick per-class λ values when we get there.
- Memories without embeddings are appended at the end rather than
  fully participating in MMR. This is a fallback, not a failure
  mode — the embed worker should drain pending records before the
  next eval run.

## References

- [RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W4
- ADR 0015 (eval harness — gates the default-on flip for this PR)
- Carbonell & Goldstein (1998), "The Use of MMR, Diversity-Based
  Reranking for Reordering Documents and Producing Summaries"
