# ADR 0043: Evaluate Jev with frozen candidates before changing search

Status: Accepted for offline evaluation only; no production ranking change.
Date: 2026-09-26

## Context

The previous LLM reranker was disabled after a small evaluation found worse
retrieval and greater latency. Jev supplies typed relevance scores at a lower
advertised cost and latency, but that does not establish a quality improvement.
The legacy substring-matching benchmark cannot reliably distinguish direct answers
from mentions, outdated facts, proposals or the wrong project's records.

## Decision

Add an isolated evaluation module and CLI. Keep production search, model routing,
configuration, storage and index schema untouched. Use frozen baseline/candidate
records, blinded evidence-backed judgments, topic-grouped dev/test separation,
paired grouped uncertainty estimates and explicit quality/latency/cost gates.

The primary proposed intervention is post-retrieval Jev reranking of bounded
query-selected passages. Head excerpts, wider-pool order and deterministic lexical
ordering are controls. This order differs from the old pre-MMR hook and must be
identified explicitly in results and any later integration proposal.

Synthetic fixtures validate the API and evaluator only. A positive result requires
at least 200 real held-out cases, 100 groups, category coverage and independent
label review. Any positive offline verdict means proceed to a live shadow check;
it is not permission to deploy. See `eval/jev/README.md` for the complete protocol.

## Consequences

- Labels and experiment preparation require work; no keyword or self-grading shortcut.
- Private queries, transcripts, raw responses and ratings remain outside version control.
- All requests and failures remain in the trial denominator. Failures return the
  captured normal ranking. Version/data/protocol hashes protect comparability.
- No new package dependency and no model-serving service.
- Summary verification, ingestion classification and deduplication remain separate
  experiments so their effects cannot be confused with reranking.
- Rollback is dropping the evaluation branch; no service rollback is needed.
