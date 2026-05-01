# Recall-quality models — recommendations and upgrade ladder

> Last updated: 2026-05-01. Tracks shipped recall-quality features
> ([RECALL-PLAN.md](../RECALL-PLAN.md)) and the model choices that
> drive them.

Memstem's recall-quality features (cross-encoder rerank, HyDE query
expansion, dedup judge, session/project summarization) need a
chat-capable LLM in addition to the embedder. The chat model is
**pluggable**: pick a provider that fits your box. This guide names
the recommended model for each feature and the next step up if
results aren't good enough.

## TL;DR

| Feature | Recommended (OpenAI) | Recommended (Ollama / local) |
|---|---|---|
| **Cross-encoder rerank** (W5, ADR 0017) | `gpt-4o-mini` | `qwen2.5:7b` |
| **HyDE query expansion** (W6, ADR 0018) | `gpt-4o-mini` | `qwen2.5:7b` |
| **Dedup judge** (ADR 0012) | `gpt-4o-mini` | `qwen2.5:7b` |
| **Session distillation** (W8, ADR 0020) | **`gpt-5.4-mini`** | `qwen2.5:7b` |
| **Project records** (W9, ADR 0021) | **`gpt-5.4-mini`** | `qwen2.5:7b` |

For rerank/HyDE/dedup, `gpt-4o-mini` is cheap, fast, and strong
enough — the model output isn't itself indexed, so the quality bar
is "reliable scoring / structured output," not "human-readable
prose."

For session distillation and project records, **the model output IS
the search target** — the summary text is what gets indexed and
returned to retrievers. Quality matters more here, which is why we
default to `gpt-5.4-mini` (the newer-generation mini-tier model with
notably better summarization output per dollar). Cost at typical
MemStem volumes is pennies per month.

`qwen2.5:7b` is the local-only equivalent across every feature —
same role, different deployment shape, zero per-call cost.

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

### Session distillation (W8, ADR 0020)

**What it does:** turns long Claude Code or OpenClaw session
transcripts into 1-paragraph rollups with structured Key entities /
Deliverables / Decisions / Status sections. The output replaces the
verbose transcript in retrieval — agents searching for the work the
session represents land on the summary.

**Recommended:** `gpt-5.4-mini` (OpenAI) / `qwen2.5:7b` (Ollama).

**Why `gpt-5.4-mini` (not `gpt-4o-mini`)** — for session
distillation, the LLM output IS the search target. Bad summaries
become permanent retrieval misses. `gpt-5.4-mini` produces
materially better entity coverage and decision clarity on
summarization benchmarks at ~5× the per-call cost of `gpt-4o-mini`,
which still works out to **single-digit dollars per month** at a
typical 5–10-substantive-sessions-per-day pace. The quality lift
is worth it for the canonical search artifact; the LLM-as-judge
features (rerank/HyDE/dedup) don't need it because their outputs
are scores or query rewrites, not indexed content.

**If results aren't good enough:**

| Step | Model | When to pick it |
|---|---|---|
| 1 | `gpt-5.4-mini` (default) | Start here. Brad's eval queries land on distillations? Done. |
| 2 | `gpt-5.4` | Better at long sessions where the answer is buried among tool calls. ~3× the cost. |
| 3 | `gpt-5` (full) | Frontier-class. Worth it only if specific failure modes survive 5.4. |

**For Ollama users:** same upgrade ladder as rerank/HyDE — `qwen2.5:7b` →
`qwen2.5:14b` → `qwen2.5:32b`. Larger local models materially improve
summarization quality (the gap between `7b` and `14b` is bigger here
than for rerank).

### Project records (W9, ADR 0021)

**What it does:** aggregates Claude Code sessions sharing a project
tag into a single `type: project` rollup at
`vault/memories/projects/<slug>.md` — canonical project name,
description, participants, deliverables, accumulated decisions.

**Recommended:** `gpt-5.4-mini` (OpenAI) / `qwen2.5:7b` (Ollama).

**Why:** project records have the same "output IS the search target"
property as session distillations. The LLM is also doing
extraction work the smaller models miss — pulling a
human-readable canonical name out of an encoded directory tag is the
kind of task where 5.4-mini's reasoning earns its keep.

**Volume note:** project records are run at much lower frequency
than session distillations (one record per project, regenerated only
when the source set changes), so the cost difference between
`gpt-5.4-mini` and the 4o-mini tier rounds to nothing in absolute
terms.

The same upgrade ladder applies for both providers — see session
distillation above.

## Cost expectations

For a 200-queries/day vault with all features default-on and
all cache misses (worst case):

| Feature | OpenAI default | Notes |
|---|---|---|
| Cross-encoder rerank (`gpt-4o-mini`) | $3-5/month | 20 candidates × 200 prompt tokens × 200 queries/day. Cache absorbs most of this in steady state. |
| HyDE (`gpt-4o-mini`) | $1/month | 1 call × 300 tokens × 200 queries/day. Cache hit rate is high — same query → same hypothesis. |
| Dedup judge (`gpt-4o-mini`) | $0.50/month | Hygiene-worker pace, not query pace. ~50 candidate pairs/week. |
| Session distillation (`gpt-5.4-mini`) | $1-5/month | One LLM call per substantive session (~15K tokens in, 500 tokens out). Cache hits when source unchanged. |
| Project records (`gpt-5.4-mini`) | $0.10-0.50/month | Regenerated only when project source set changes. Typically a few calls per week. |

Realistic with caches warm: **$2-8/month total**.

For Ollama users: $0/month + the cost of the GPU/RAM you already
have. Note that summarization is materially heavier than rerank/HyDE
on local hardware — a CPU-only Ollama can take 30-90 seconds per
session distillation, vs. low-second-digit on a GPU. Plan for that
when sizing.

## Related ADRs

- [ADR 0008 — Tiered memory and importance scoring](decisions/0008-tiered-memory.md)
- [ADR 0012 — LLM-as-judge dedup](decisions/0012-llm-judge-dedup.md)
- [ADR 0015 — Recall-quality eval harness](decisions/0015-eval-harness.md)
- [ADR 0017 — Cross-encoder rerank](decisions/0017-cross-encoder-rerank.md)
- [ADR 0018 — HyDE query expansion](decisions/0018-hyde-query-expansion.md)
- [ADR 0020 — Session distillation writer](decisions/0020-session-distillation-writer.md)
- [ADR 0021 — Project records](decisions/0021-project-records.md)
