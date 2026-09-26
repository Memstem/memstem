# 0044 — Shadow-mode Jev reranking

- Status: accepted (shadow only; served ranking unchanged)
- Date: 2026-09-26
- Builds on: 0017 (LLM reranker, disabled 2026-06-16), 0043 (offline Jev evaluation, PR #197)

## Context

The ADR 0017 Gemma reranker was disabled in June after a 16-query A/B found it
slower (1.5 s → 9.2 s) and slightly worse. It scored candidates one at a time
and read only the first 4,000 characters of each memory.

On 2026-09-26 an offline pilot (ADR 0043) reranked frozen candidate pools for
50 held-out real queries with Jev (`typesafe/jev-1.13`, OpenRouter's decisions
API): one batched call per query, with each candidate reduced to the three
fixed windows that share the most words with the query. Against model-labelled
relevance:

| method (same passages) | nDCG@10 | gain vs normal search, 95% CI |
|---|---|---|
| normal search | 0.62 | — |
| Jev | 0.78–0.79 | +0.15 to +0.17, CI lower bound ≥ +0.10 |
| Gemma 4 26B | 0.78–0.79 | +0.16, not distinguishable from Jev |
| Qwen3-Reranker-0.6B | 0.75–0.77 | +0.12 to +0.14, not distinguishable from Jev |

A relabel with a wider evidence view (semantic chunks from whole documents)
left Jev's gain unchanged, ruling out an answer key tilted toward the keyword
windows. Hit@10 was unchanged (~98–100%); the gain is better ordering: the
top-1 result was a direct answer 67% of the time vs 52%. Jev's reranking call
took 0.21 s p50 / 0.27 s p95 (Gemma: 4.3 s / 8.3 s via OpenRouter), at about
$0.34 per 1,000 searches. Preparation of the passages cost a further ~1 s p50,
since fixed here.

The pilot reused one 50-query sample with model-made labels. Before any
served change we need live-traffic evidence: failure rate, real latency, and
how often and how Jev would change what agents see.

## Decision

Add an opt-in shadow mode (`search.jev_shadow`, off by default):

1. After an HTTP or MCP search returns (`shadow_client` set by the server),
   a job is queued to one background daemon thread per process. The served
   result list is never modified; queue-full, filter or setup failures drop
   the job silently, and `Search` guards the submit call.
2. The worker re-runs the query with `pool_size` (20), appends any served hit
   the wider search missed (the pilot's pool shape), selects passages, and
   sends one batched decisions request (2 s timeout, 30 KB cap, 1,600-char
   starting excerpt budget shrinking by 3/4 to fit) — the pilot's settings.
3. Each run is appended to `<vault>/_meta/jev-shadow.db` (SQLite WAL, 0600):
   query, served IDs, pool IDs, Jev order and scores, body hashes, timings
   (pool search, preparation, API), status and cost. The ledger is shared by
   the daemon and all `memstem mcp` processes; `daily_budget_usd` (0.50) is
   enforced against it, with a 0.002 USD reserve for unknown-cost calls.
4. Skipped: watchdog probes (`types` containing `__watchdog__`), searches with
   `limit < 3`, and credential-retrieval queries. Only selected passages (and
   titles) leave the host, redacted for API keys, private keys, SQL `-P`
   arguments, `password=`-style assignments and URL credentials; each window
   is first widened to fully contain any secret it overlaps.
5. Passage selection is one casefolded regex pass per document, reused for
   every budget, so multi-megabyte transcripts are scanned once. On the pilot
   documents it reproduces the pilot selector for 98% of document/budget pairs
   (differences: words cut by a window edge) and cut preparation from
   1.05 s to 0.19 s p50 (2.5 s → 0.49 s p95).
6. `scripts/jev_shadow_report.py` summarizes volume, failures, latency, cost
   and would-change rates.

## Evaluation (after about a week)

- Operations: fallback/error rate ≤ 1%, preparation + API p95 ≤ 1 s, cost.
- Quality: relevance-label a sample of logged pools (served order vs Jev order)
  with the pilot's labelling procedure plus a small human review; inspect every
  case where Jev drops a served top-3 hit out of its top 10.
- Serving Jev's order needs a separate decision and ADR. Gemma 4 and
  Qwen3-Reranker-0.6B remain alternatives if Jev's single-vendor alpha API
  proves unreliable; the scoring call is isolated in `_post`/`parse_scores`.

## Consequences

- One extra search (pool) per eligible query and about $0.0004 of Jev spend;
  at current volume (~55 real searches/day) under $1/month.
- Query text and redacted passages from real searches go to OpenRouter and
  TypeSafe. Embeddings and hygiene already send memory text to OpenRouter;
  this adds one provider.
- `jev-shadow.db` holds query text; it is local, 0600, and not indexed.
- Rollback: set `search.jev_shadow.enabled: false` (or remove the block) and
  restart; delete `_meta/jev-shadow.db` if the data is no longer wanted.
