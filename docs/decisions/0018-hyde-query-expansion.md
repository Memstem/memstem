# ADR 0018: HyDE query expansion at retrieval time

Date: 2026-04-30
Status: Accepted

## Context

Vec retrieval ranks documents by their semantic distance to the query
in embedding space. The bi-encoder embeds query and document
independently, so retrieval quality depends on the two sides sharing
embedding-space neighbors. Most queries do — "what gateway port does
Ari run on" lands close to documents that mention "Ari port" or "18789"
because the bi-encoder learned those associations during training.

But some queries don't share embedding-space neighbors with their
correct answers, even when the answer is in the vault. The classic
failure mode is the **procedural query / declarative answer** mismatch:

- Query: *"how do I send a Telegram message"*
- Answer document: *"Use `bash ~/scripts/tg-send 'Your message'`. The
  script reads the bot token from ..."*

The query is a question; the document is an imperative instruction.
Vocabulary diverges ("how do I" → "use", "send" → "run", "Telegram
message" → "tg-send"). The bi-encoder hasn't been trained on
Memstem-specific vocabulary, so it doesn't know `tg-send` is the
canonical procedure for "send a Telegram message". The result: vec
retrieval misses; BM25 might catch it via "Telegram" overlap, but a
rank-7 BM25 hit doesn't beat 50 noise documents in the fused result.

Memstem's eval set surfaces this directly. The current weakest class
is **conceptual** at MRR 0.400, R@10 0.667; the only zero-rank failure
in the entire 12-query baseline is `conceptual_dedup_pipeline` ("how
does Memstem dedup work") — a procedural-shaped question whose answer
documents talk about ADR pipelines, code modules, and "Layer 1/2/3"
without ever rephrasing the question.

The IR literature has a standard answer: **Hypothetical Document
Embeddings (HyDE)** [Gao et al., "Precise Zero-Shot Dense Retrieval
without Relevance Labels", 2022]. The pipeline:

1. Take the user's query.
2. Ask a generative LLM to write a *hypothetical answer* — a passage
   that would directly resolve the query, even if it's wrong on facts.
3. Embed the *hypothetical answer*, not the original query.
4. Use that embedding for vec retrieval.

The hypothetical answer shares vocabulary with the *real* answer in
the vault, even when the original query doesn't. The LLM doesn't need
to be right about the facts — it just needs to produce passage-shaped
text that lands near the actual answer in embedding space. A
hallucinated step ("run `tg-cli send <message>`") still pulls vec
retrieval toward the documents that describe the real `tg-send`
script, because the bi-encoder maps both to the
"send-a-message-via-CLI" region of embedding space.

[RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W6 specifies:

- `core/hyde.py` with `expand_query(query, llm) -> str` and
  `should_expand(query) -> bool` (gates out short / exact-string
  queries).
- Reuse the Ollama client from `dedup_judge`.
- Per-query cache on the hypothetical answer.
- Acceptance: ≥20% MRR improvement on procedural-class queries; no
  factual-class regression.
- Default-off until eval passes.

## Decision

Add `core/hyde.py` with a `HydeExpander` ABC and three implementations
(`NoOpExpander`, `StubExpander`, `OllamaExpander`), wire it into
`Search.search` behind an optional `use_hyde` parameter, and add a
`hyde_cache` SQLite table for `(query_hash, judge) → hypothesis`
memoization.

### Module shape

`HydeExpander` mirrors the dedup-judge / reranker pattern (ADR 0012,
ADR 0017). Two methods:

```python
class HydeExpander(ABC):
    name: str = "abstract"

    @abstractmethod
    def expand(self, query: str) -> str:
        """Return a hypothetical-answer passage for the query."""

    def should_expand(self, query: str) -> bool:
        """Return True iff the query benefits from hypothetical expansion."""
```

`expand` produces the hypothetical answer. `should_expand` is the gate
that prevents wasted LLM calls on queries that don't benefit.

Implementations:

- **NoOpExpander**: returns the query unchanged. Used as the silent
  fallback when `use_hyde=True` is set on a `Search` without a
  configured expander.
- **StubExpander**: in-memory verdicts for tests. Mirrors
  `StubReranker` — tests register `(query → hypothesis)` and the
  orchestrator returns exactly that.
- **OllamaExpander**: production expander. Calls Ollama
  `/api/generate` with a passage-generation prompt, returns the
  trimmed response. Default model matches the dedup judge and
  reranker for consistency.

### `should_expand` gating

HyDE adds latency to every query it fires on. The gate prunes queries
that don't benefit:

- **Length gate**: queries shorter than 3 words are typically exact
  lookups ("ari port", "rrf k") where the original query already
  shares vocabulary with the answer. HyDE adds noise.
- **Quoted-string gate**: queries containing `"..."` signal user
  intent for exact-string match. HyDE's hypothesis would dilute that.
- **Boolean operators**: queries with explicit AND/OR/NOT or `+`/`-`
  prefixes are structured queries; HyDE doesn't compose with them.
- **Identifier-shape gate**: queries that look like a UUID, a file
  path, or a hex hash get exact-match treatment, not expansion.

Gates are conservative defaults — the operator can override with an
explicit `force=True` parameter (deferred; not in v1). The gate is
implemented as a stateless function so callers can probe it without
instantiating the full expander stack.

### Search wiring

`Search.__init__` grows one optional kwarg:

- `hyde: HydeExpander | None = None`. Defaults to `None`. When
  `None`, `Search` constructs a `NoOpExpander` at first need so the
  search path stays branch-free.

`Search.search` grows one optional kwarg:

- `use_hyde: bool = False`. When `False` (the default), HyDE is
  skipped entirely — the existing pipeline is unchanged. When `True`,
  the search path:
  1. Calls `self.hyde.should_expand(query)`. On `False`, falls
     through to the original query (HyDE is silently skipped).
  2. On `True`, calls `self.hyde.expand(query)` (cache-aware) and
     embeds the result as the vec query embedding.
  3. **BM25 still uses the original query.** HyDE's value is replacing
     embedding-space proximity, not lexical match — and the original
     query's keywords are a strong precision signal that the
     hypothetical answer dilutes.

Pipeline order (with all stages enabled):

```
BM25 retrieval (on original query)
  ‖
  ‖   [HyDE: expand query → hypothesis → embed hypothesis]
  ‖
vec retrieval (on hypothesis embedding)
  → RRF combine
  → _materialize (importance boost + sort + truncate)
  → rerank top_n   (ADR 0017)
  → mmr_rerank     (ADR 0016)
  → truncate to limit
```

HyDE runs *first*, before any retrieval. The downstream stages
(rerank, MMR) operate on the same materialized pool regardless of
whether HyDE fired — they don't need to know.

### Cache

OllamaExpander calls are deterministic on `(query, judge)` — same
query, same model, same prompt → same hypothesis. The cache holds
the hypothesis text so cache hits skip the LLM round trip.

Schema (migration v10):

```sql
CREATE TABLE IF NOT EXISTS hyde_cache (
    query_hash TEXT NOT NULL,
    judge TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (query_hash, judge)
);
CREATE INDEX IF NOT EXISTS idx_hyde_cache_ts ON hyde_cache(ts);
```

- `query_hash`: SHA-256 hex of the raw query string. Hash rather
  than full text because the hypothesis-text column is the bulky one
  and hash collision probability is negligible.
- `judge`: which expander produced the hypothesis. Lets us cache
  results from multiple model variants side-by-side; swapping models
  invalidates the right rows automatically.
- No `body_hash` (unlike `rerank_cache`): HyDE's input is just the
  query, not any specific document.
- Non-canonical: losing the table costs first-call latency, not
  correctness.

Cache lookup happens inside `HydeExpander.expand_cached` (a base-
class helper). NoOp bypasses the cache (its output is constant). Stub
uses the cache so tests can verify hit/miss paths.

### Embedder integration

The HyDE-expanded query is embedded by the same `Embedder` instance
the search path already uses. No new embedder configuration; no
separate model. This keeps the dependency surface flat — HyDE's
"swap the embedder input" approach composes cleanly with the
existing nomic-embed-text wiring.

When the embedder is `None` (BM25-only setups), HyDE is silently
skipped: there's no vec query to expand, so there's no work to do.
The decision is made *before* the LLM round trip so we don't pay for
a hypothesis that's about to be thrown away.

### Fallback semantics

- **`use_hyde=False`** (default): HyDE skipped entirely. Behavior is
  exactly pre-HyDE.
- **`use_hyde=True`, `hyde is None` at construction**: NoOpExpander
  returns the query unchanged. Result: no behavior change. The
  flag is honored, the work is just trivial.
- **`use_hyde=True`, `should_expand=False`**: original query used
  for vec retrieval. No LLM call. Logged at DEBUG.
- **`use_hyde=True`, embedder is None**: HyDE skipped; BM25-only
  retrieval runs on the original query.
- **OllamaExpander unreachable**: `expand` returns `""` after
  logging the error; `Search` detects the empty string and falls back
  to the original query for embedding. Search completes; eval
  surfaces any quality regression.

### Configuration

Phase 1 (this PR): `use_hyde` is a per-call parameter on
`Search.search`. Tests pass it explicitly.

Phase 2 (deferred until eval shows a default-on flip is warranted):
add `ranking.hyde.{enabled, model}` to `SearchConfig`. Until then,
callers thread `False` and search behaves identically to pre-HyDE.

## Cost

Per query, with HyDE enabled and a cold cache:

- **OllamaExpander**: one `/api/generate` call producing ~80-150
  tokens of hypothesis. With `qwen2.5:7b` on Brad's box this is
  ~600-900ms — a single round trip, not a sweep.
- **Embedding the hypothesis**: same cost as embedding the original
  query (~50ms with nomic-embed-text). No additional cost beyond what
  we already pay.
- **Cache hit**: ~5ms SQLite lookup; no LLM.

The "≥20% MRR improvement on procedural-class queries" gate applies
to default-on; this PR ships HyDE off by default. Cold-cache
latency on first run after a config flip is documented and
operator-visible.

Compared to W5 cross-encoder rerank, HyDE is one LLM call per query
(not N calls per query), so the cold-cache latency is dramatically
better. The cache is also more effective — the same query hits the
cache regardless of which documents are in the vault.

## Schema

One new migration (v10) adding the `hyde_cache` table. No changes
to `memories`, `memories_vec`, or any prior table. The cache is
non-canonical.

## Tests

- **Unit tests** in `tests/test_hyde.py`:
  - `query_hash` + `cache_lookup` + `cache_write` round-trip.
  - `should_expand` behavior across the four gates (length,
    quoted string, boolean op, identifier-shape).
  - `NoOpExpander.expand` returns the query unchanged; never
    touches the cache.
  - `StubExpander.set_hypothesis` + `expand` round-trip via
    `expand_cached`.
  - Cache hit on second call skips `expand`.
  - `OllamaExpander` prompt-parsing: trims whitespace, handles
    fenced code blocks, handles empty response.
  - `OllamaExpander` HTTP failure → empty hypothesis (caller
    falls back).
- **Integration tests** in `TestSearchHyde`:
  - `use_hyde=False` → HyDE not invoked.
  - `use_hyde=True` with NoOp → original query used.
  - `use_hyde=True` with stub that returns a hypothesis with
    different keywords → vec retrieval uses the hypothesis
    embedding.
  - `use_hyde=True` with embedder=None → HyDE skipped silently.
  - `use_hyde=True` + `should_expand=False` → original query used.

The Ollama-side tests use `requires_ollama` (skipped in CI unless
an Ollama instance is reachable).

## Eval gate

Per [RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W6 and ADR 0015:

A PR that flips HyDE on by default must show:
- ≥20% relative MRR improvement on the **procedural** class, AND
- no per-class regression worse than 5% relative on **factual**, AND
- no aggregate MRR regression.

This PR ships HyDE off by default; the flip is a follow-up gated on
eval data, not on this ADR.

## Rationale

- **Why an ABC + multiple impls** instead of a single concrete
  expander: matches the dedup-judge / reranker pattern. Tests use
  Stub; production uses Ollama; NoOp is the silent fallback. Same
  shape, same testing muscle memory.
- **Why default-off**: eval-gated default-on is the project's
  recall-quality contract per ADR 0015. HyDE has well-known
  asymmetries (helps procedural, can hurt factual when the
  hypothesis is plausible-but-wrong); the gate exists for exactly
  this reason.
- **Why per-call `use_hyde` rather than always-on with gate**:
  separates "the operator wants HyDE for this call" from "the gate
  thinks the query benefits". Two layers of decisions, both
  observable, both testable. The operator can force-disable by
  passing `False`; the gate can force-skip on a per-query basis.
- **Why cache by `(query_hash, judge)`** without `body_hash`: HyDE
  expands the query, not a query/document pair. The hypothesis is
  document-independent.
- **Why BM25 stays on the original query**: lexical overlap is a
  strong precision signal; HyDE's hypothesis is rich on semantic
  context but adds noise tokens that hurt BM25 ranking. The
  literature confirms: HyDE wins on dense retrieval, loses on BM25.
  Splitting the signals is the canonical implementation.
- **Why `expand` returns `str` rather than `list[float]`**: the
  caller (Search) re-uses its existing `Embedder` to embed the
  hypothesis. Coupling HyDE to a specific embedding API would
  break the abstraction. The "hypothesis is text" interface lets
  callers embed it any way they want.

## Consequences

**Pros:**

- One flag (`use_hyde=True`) turns on the standard fix for the
  procedural-vocabulary-mismatch failure mode.
- Composes with W5 (rerank) and W4 (MMR) — they operate on the
  retrieval pool that HyDE built; they don't need to know HyDE
  ran.
- One LLM call per query, not N — much better cold-cache latency
  than rerank.
- Cache is high-hit-rate: same query hits regardless of vault
  state.
- Reversible: `use_hyde=False` reverts to pre-HyDE behavior.
  Drop the `hyde_cache` table at any time without losing
  canonical state.

**Cons:**

- LLM hallucinations can degrade results when the hypothesis is
  plausibly-wrong about a factual query — the hypothesis pulls vec
  retrieval toward documents about the wrong fact. Mitigated by the
  per-class eval gate.
- Adds a per-query LLM round trip on cold cache. First-time-on
  users see slow queries until the cache warms.
- Default-off; the lift only shows up in the follow-up PR's eval.
  Users who want it today have to opt in per call.

## Open questions

- **Hypothesis length**: longer hypotheses share more vocabulary
  with potential answers but are noisier. The literature converges
  on "one paragraph". The prompt template targets that without a
  hard token cap.
- **Multi-hypothesis HyDE**: the original paper averages embeddings
  from multiple sampled hypotheses. We ship single-hypothesis to
  keep cost predictable; multi-hypothesis is a follow-up if eval
  shows headroom.
- **Pre-warming**: the cache is empty on a fresh install. A future
  PR could prewarm common queries; today, queries warm naturally
  through use.

## References

- [RECALL-PLAN.md](../../RECALL-PLAN.md) §3 W6
- ADR 0008 ([0008-tiered-memory.md](./0008-tiered-memory.md)) — importance
  boost HyDE composes with
- ADR 0012 ([0012-llm-judge-dedup.md](./0012-llm-judge-dedup.md)) —
  judge ABC pattern HyDE mirrors
- ADR 0015 ([0015-eval-harness.md](./0015-eval-harness.md)) — eval gate
- ADR 0016 ([0016-mmr-diversification.md](./0016-mmr-diversification.md)) —
  MMR stage HyDE feeds into
- ADR 0017 ([0017-cross-encoder-rerank.md](./0017-cross-encoder-rerank.md)) —
  rerank stage HyDE feeds into
- Gao et al. (2022), "Precise Zero-Shot Dense Retrieval without
  Relevance Labels" — HyDE reference
