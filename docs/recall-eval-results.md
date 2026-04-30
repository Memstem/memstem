# Recall-quality eval results — 2026-04-30

> Captures the first end-to-end measurement of W5 (cross-encoder
> rerank, ADR 0017) and W6 (HyDE, ADR 0018) against Brad's live vault
> using `gpt-4o-mini` via OpenAI. The eval gates from ADR 0015 are
> explicit; this document records that **neither feature passes the
> default-on gate on this vault as configured**, and walks through
> what the per-class data points to for follow-up tuning.

## Setup

- **Vault:** `~/memstem-vault` (1,268 memories)
- **Embedder:** `nomic-embed-text` via Ollama (768-dim)
- **Chat model:** `gpt-4o-mini` via OpenAI for both W5 and W6
- **Query set:** `eval/queries.yaml` (12 queries, 4 classes)
- **Configuration:**
  - W5: `rerank_top_n=20`
  - W6: `use_hyde=True` (no procedural gating override)
- **Tooling:** ad-hoc `eval_compare.py` running each config against
  `Search.search` directly. The standard `scripts/run_eval.py` lacks
  the CLI flags to A/B providers (a follow-up).

## Headline numbers

| Run | MRR | Δ MRR | R@3 | R@10 | Found | Total elapsed |
|---|---|---|---|---|---|---|
| Baseline | **0.737** | — | 0.750 | 0.917 | 11/12 | 10.6s |
| W5 (rerank only) | 0.648 | **-12.0%** | 0.833 | 0.917 | 11/12 | 73s |
| W6 (HyDE only) | 0.686 | **-7.0%** | 0.750 | 0.917 | 11/12 | 46s |
| W5 + W6 | 0.606 | **-17.7%** | 0.833 | 0.917 | 11/12 | 22s* |

\* Cache was warm by run 4; cold-cache W5+W6 would be ~120s.

## Per-class MRR

| Class | Baseline | W5 only | W6 only | W5+W6 |
|---|---|---|---|---|
| conceptual (n=3) | 0.400 | **+25.0%** | -4.8% | +25.0% |
| factual (n=4) | 0.786 | -15.2% | **+6.1%** | -15.2% |
| historical (n=2) | 1.000 | **-44.4%** | -25.0% | -44.4% |
| procedural (n=3) | 0.833 | +0.0% | -10.0% | -20.0% |

## What the data says

The aggregate MRR drop hides asymmetric per-class effects — exactly
what ADR 0015's per-class breakdown was designed to surface.

**W5 (cross-encoder rerank):**
- Wins on **conceptual** (+25%), the class with the weakest baseline.
  The conceptual_dedup_pipeline failure improves; cross-encoder
  judgment helps when the query and answer don't share vocabulary.
- Loses on **historical** (-44%) and **factual** (-15%). These are
  the classes where the *right* answer is short and decision-shaped.
  The cross-encoder appears to favor longer documents that discuss
  the topic in volume over short documents that directly answer.
- **R@3 actually improves** (0.750 → 0.833) — the rerank still puts
  the right answer in the top 3 *more often*. But it doesn't put it
  at rank 1 as often, which is what MRR penalizes.

**W6 (HyDE):**
- Wins on **factual** (+6%) — opposite of the original hypothesis,
  which expected procedural to benefit most.
- Loses on **historical** (-25%) and **procedural** (-10%). The LLM
  doesn't know Memstem-specific vocabulary (`tg-send`, agent names,
  internal commands), so its hypothesis pulls vec retrieval toward
  generic-Telegram-app territory rather than the right answer.

This is the canonical **HyDE-on-domain-specific-corpus failure
mode**: the hypothesizer hallucinates plausible-but-wrong-vocabulary,
which is fine for general-knowledge corpora and harmful for
private-vocabulary ones.

## Latency

Independent of quality, both features fail the p95 < 500ms gate:

- W5: ~6s/query (20 candidates × ~300ms each)
- W6: ~4s/query (1 LLM call producing a paragraph)

Even with a warm cache the steady-state latency exceeds the budget.
On cold cache it's worse — 32s/query for W5 was observed before the
truncation fix.

## Conclusion

Neither feature is shipped default-on. The eval gate works as
designed: data-driven, per-class, blocks regressions hidden by
aggregate metrics.

The features stay available for callers who explicitly want them —
e.g. for a query class where the asymmetry favors the lift —
but the system-wide default remains pre-W4 + W3 + W1: RRF + importance
+ optional MMR.

## Bug discovered during this work

The 1.7 MB and 1.5 MB `Infrastructure — Extended Context` memories
caused 400 Bad Request errors from the OpenAI rerank API on the
first eval run because the document body exceeded the model's
context window. Fixed by truncating to `MAX_RERANK_BODY_CHARS` in
the rerank prompt; cache key still hashes the full body so cache
invalidation is unaffected.

## Follow-up tuning ideas (not in this PR)

If a future PR wants to make either feature pass its gate, the per-
class data points to specific things to try:

**For W5:**

1. **Drop `rerank_top_n` from 20 to 10.** Fewer candidates = less
   chance for the cross-encoder to demote the correct top-1.
   Smaller top-N also halves the latency.
2. **Strengthen the rerank prompt** to prefer short, direct-answer
   documents over longer-on-topic discussions. The current prompt
   doesn't bias toward this.
3. **Try `gpt-4.1-mini` or `gpt-4o`.** The small-model judgment
   error rate on historical queries might just be a model-capacity
   issue.

**For W6:**

1. **Tighten `should_expand` to skip historical and procedural
   classes.** HyDE only fires on conceptual / factual queries —
   exactly the classes where the data says it's roughly neutral or
   helpful.
2. **Add a "vocabulary aware" prompt variant** that asks the LLM
   to use *generic* documentation vocabulary rather than specific
   tool names. Tradeoff: less retrieval-friendly on the queries it
   does help.
3. **Try multi-hypothesis HyDE** (paper's original recipe averages
   embeddings from N=8 sampled hypotheses). 8× the LLM cost but
   averages out the worst hallucinations.

**For the eval set:**

The 12-query set is small enough that one query swinging changes a
class's MRR by 25-50%. Expanding the set to 30 hand-curated queries
(8 per class) would make per-class metrics more stable and
distinguish real lifts from sampling noise.

## Replication

The ad-hoc comparison script lives at `/tmp/eval_compare.py` (not
checked in — the standard `scripts/run_eval.py` is what's
version-controlled). Per-config JSON output is at:

- `/tmp/eval-baseline.json`
- `/tmp/eval-w5_rerank_only.json`
- `/tmp/eval-w6_hyde_only.json`
- `/tmp/eval-w5+w6.json`

The cache state at run time:
- `rerank_cache`: 281 rows pre-run, 115 of which were
  zero-score from the 400-error pollution; cleared before re-run.
- `hyde_cache`: 12 rows from a prior partial run; cleared.

A follow-up PR should add `--rerank-provider`, `--rerank-model`,
`--rerank-top-n`, `--use-hyde`, `--hyde-provider`, `--hyde-model`
flags to `scripts/run_eval.py` so this kind of comparison is a
single command, not an ad-hoc Python script.
