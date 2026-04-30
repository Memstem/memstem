# Recall-quality models — recommendations and upgrade ladder

> Last updated: 2026-04-30. Tracks shipped recall-quality features
> ([RECALL-PLAN.md](../RECALL-PLAN.md)) and the model choices that
> drive them.

Memstem's recall-quality features (cross-encoder rerank, HyDE query
expansion, dedup judge) need a chat-capable LLM in addition to the
embedder. The chat model is **pluggable**: pick a provider that fits
your box. This guide names the recommended model for each feature
and the next step up if results aren't good enough.

## TL;DR

| Feature | Recommended (OpenAI) | Recommended (Ollama / local) |
|---|---|---|
| **Cross-encoder rerank** (W5, ADR 0017) | `gpt-4o-mini` | `qwen2.5:7b` |
| **HyDE query expansion** (W6, ADR 0018) | `gpt-4o-mini` | `qwen2.5:7b` |
| **Dedup judge** (ADR 0012) | `gpt-4o-mini` | `qwen2.5:7b` |

`gpt-4o-mini` is the safe default for cloud-backed setups: cheap,
fast, strong enough for all three jobs. `qwen2.5:7b` is the
local-only equivalent — same role, different deployment shape.

If results don't meet the eval bar, walk the upgrade ladder per
feature below.

## Why a chat model

The embedder (`nomic-embed-text` for Ollama, `text-embedding-3-small`
for OpenAI) is a fingerprinter — it turns text into a vector but
can't read or write. Three of Memstem's recall-quality features need
a model that can:

- **Cross-encoder rerank** — read `(query, document)` pairs and score
  relevance on a 0-100 scale.
- **HyDE query expansion** — read a query and write a hypothetical
  one-paragraph answer.
- **Dedup judge** — read two candidate-duplicate records and decide
  if they're DUPLICATE / CONTRADICTS / RELATED_BUT_DISTINCT /
  UNRELATED.

Any of these three can be turned off by leaving the feature flag
unset; the embedder + RRF + importance + MMR pipeline keeps working.
You only need a chat model for the features you turn on.

## Provider options

### OpenAI (cloud)

Use when: the host doesn't have GPU resources, or when you want a
predictable per-query cost without managing local model
infrastructure.

Setup:

```bash
# One of:
export OPENAI_API_KEY=sk-...
# or
memstem auth set openai sk-...
```

Then construct any of:

```python
from memstem.core.rerank import OpenAIReranker
from memstem.core.hyde import OpenAIExpander

reranker = OpenAIReranker(model="gpt-4o-mini")
expander = OpenAIExpander(model="gpt-4o-mini")
```

Both classes accept `base_url` for OpenAI-compatible endpoints
(Together, LM Studio, vLLM, etc.) so a self-hosted compatible API
works the same way.

### Ollama (local)

Use when: the host has the RAM/VRAM budget for a 4-8 GB model and
you want zero per-query cost.

Setup:

```bash
ollama pull qwen2.5:7b
```

Then:

```python
from memstem.core.rerank import OllamaReranker
from memstem.core.hyde import OllamaExpander

reranker = OllamaReranker(model="qwen2.5:7b")
expander = OllamaExpander(model="qwen2.5:7b")
```

### NoOp (everything off)

Both `Search`'s `reranker` and `hyde` slots default to NoOp
implementations that pass the original query through unchanged.
This is the v0.1 behavior — you don't have to configure either to
keep using Memstem.

## Recommendations and upgrade ladder

### Cross-encoder rerank (W5)

**What it does:** after RRF + importance, re-scores the top-N
candidates and re-sorts. The biggest documented precision lift in
the IR literature.

**Recommended:** `gpt-4o-mini` (OpenAI) / `qwen2.5:7b` (Ollama).

**Why:** rerank fires on every search query that has it enabled
(potentially 20+ scoring calls per query). The rate-limit-friendly
choice is the cheapest model that produces stable 0-100 scores.
`gpt-4o-mini` does this reliably and ~3-4× faster than `gpt-4o`.

**If results aren't good enough** (per [ADR 0015 eval](decisions/0015-eval-harness.md)):

| Step | Model | When to pick it |
|---|---|---|
| 1 | `gpt-4o-mini` (default) | Start here. Hit ≥15% MRR lift on the eval set with no per-class regression > 5%? Done. |
| 2 | `gpt-4.1-mini` | The next-gen mini. Slightly better judgment quality at similar cost. Try this if `gpt-4o-mini` plateaus before the 15% bar. |
| 3 | `gpt-4o` | A real upgrade. ~3-5× the cost and ~2-3× the latency, but better at edge cases (long documents, ambiguous queries). |
| 4 | `gpt-4.1` | Frontier. Only worth it if precision is the bottleneck and cost isn't a constraint. |

**What NOT to use:** reasoning models (`o1`, `o3-mini`, `o3`).
They're slow (1-3s per call) and overkill for "score 0-100."

**For Ollama users**, the equivalent ladder:

| Step | Model | When to pick it |
|---|---|---|
| 1 | `qwen2.5:7b` (default) | ~5 GB. The standard local choice. |
| 2 | `qwen2.5:14b` | ~9 GB. Better quality if your box has the RAM. |
| 3 | `qwen2.5:32b` | ~20 GB. Frontier local quality. Needs a real GPU. |

### HyDE query expansion (W6)

**What it does:** before vec retrieval, asks the LLM to write a
hypothetical answer passage. The hypothesis is embedded in place of
the original query so vec search lands on documents that share
*answer vocabulary* with the question.

**Recommended:** `gpt-4o-mini` (OpenAI) / `qwen2.5:7b` (Ollama).

**Why:** HyDE is one LLM call per query — cheap. Quality matters
because the hypothesis directly drives retrieval. `gpt-4o-mini` is
strong at "write a passage that sounds like documentation about X"
even when it's wrong on facts (which is fine for HyDE — the
embedder cares about vocabulary, not truth).

**If results aren't good enough** (per ADR 0015):

| Step | Model | When to pick it |
|---|---|---|
| 1 | `gpt-4o-mini` (default) | Start here. ≥20% MRR lift on procedural-class queries, no factual regression > 5%? Done. |
| 2 | `gpt-4.1-mini` | Better at vocabulary fidelity — names the right tools/files/commands more often. |
| 3 | `gpt-4o` | When the failure mode is "hypothesis is too generic." Bigger models produce more specific passages. |
| 4 | Multi-hypothesis HyDE | Defer to a future PR. Average embeddings from multiple sampled hypotheses; the original HyDE paper used 8 samples. Adds 8× the LLM cost. |

**What to watch for:** HyDE can *hurt* factual queries when the
hypothesis is plausibly-wrong. The eval's per-class breakdown
catches this — if `factual` MRR drops > 5%, HyDE is doing damage on
those queries even if `procedural` improves. The fix is usually
prompt-engineering (tightening `prompts/hyde.txt`), not a bigger
model.

**For Ollama users:** same ladder as rerank — `qwen2.5:7b` →
`qwen2.5:14b` → `qwen2.5:32b`.

### Dedup judge (Layer 3, ADR 0012)

**What it does:** for each pair of body-similar memory candidates,
classifies into DUPLICATE / CONTRADICTS / RELATED_BUT_DISTINCT /
UNRELATED. The output drives the future "apply verdicts" step that
sets `deprecated_by` and `valid_to`.

**Recommended:** `gpt-4o-mini` (OpenAI) / `qwen2.5:7b` (Ollama).

**Why:** dedup is a four-way classification on short bodies; the
quality bar is "doesn't false-positive on near-paraphrases that
actually disagree on a numeric value." `gpt-4o-mini` is reliable
here.

**If results aren't good enough:**

The eval target for dedup is precision — false DUPLICATE verdicts
destroy information. If the verdicts include false positives:

| Step | Model | When to pick it |
|---|---|---|
| 1 | `gpt-4o-mini` (default) | Start here. False-positive rate < 2%? Done. |
| 2 | `gpt-4.1-mini` | Better at "same name, different referent" disambiguation. |
| 3 | `gpt-4o` | Frontier-class judgment. Worth it if the audit log shows recurring false positives on long-tail cases. |

Note: today the dedup judge is **Ollama-only** (no `OpenAIDedupJudge`
yet). The OpenAI variant is a follow-up PR — same shape as
`OpenAIReranker` and `OpenAIExpander`.

## Cost expectations

For a 200-queries/day vault with all three features default-on and
all cache misses (worst case):

| Feature | OpenAI (`gpt-4o-mini`) | Notes |
|---|---|---|
| Cross-encoder rerank | $3-5/month | 20 candidates × 200 prompt tokens × 200 queries/day. The cache absorbs most of this in steady state. |
| HyDE | $1/month | 1 call × 300 tokens × 200 queries/day. Cache hit rate is high — same query → same hypothesis. |
| Dedup judge | $0.50/month | Hygiene-worker pace, not query pace. ~50 candidate pairs/week. |

Realistic with caches warm: $1-3/month total.

For Ollama users: $0/month + the cost of the GPU/RAM you already
have.

## Related ADRs

- [ADR 0008 — Tiered memory and importance scoring](decisions/0008-tiered-memory.md)
- [ADR 0012 — LLM-as-judge dedup](decisions/0012-llm-judge-dedup.md)
- [ADR 0015 — Recall-quality eval harness](decisions/0015-eval-harness.md)
- [ADR 0017 — Cross-encoder rerank](decisions/0017-cross-encoder-rerank.md)
- [ADR 0018 — HyDE query expansion](decisions/0018-hyde-query-expansion.md)
