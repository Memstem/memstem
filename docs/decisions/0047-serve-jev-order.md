# 0047 — Serve Jev's order (opt-in), shadow ledger retained

- Status: accepted (opt-in per vault; default off)
- Date: 2026-09-30
- Builds on: 0044 (shadow-mode Jev reranking), 0043 (offline evaluation)

## Context

Shadow mode ran on brads-server from 2026-09-26. After four days (348 runs)
a seeded sample of 60 distinct real queries was relevance-labelled blind by
Gil with the pilot's rubric, and Jev's would-be order was compared with the
order actually served:

| | served | Jev |
|---|---|---|
| nDCG@10 | 0.49 | 0.82 (+0.33, 95% CI +0.27 to +0.39) |
| length-matched nDCG@k | 0.54 | 0.80 (+0.26, CI +0.20 to +0.33) |
| top-1 is a direct answer | 17/60 | 30/60 |
| top-1 irrelevant | 11/60 | 3/60 |
| harmful top-1 | 0 | 0 |

Jev was better on 54 queries, worse on 3 (largest loss −0.11), same on 1.
The wider pool alone gave +0.11 and a word-overlap reorder +0.21, so part of
the gain is looking past MMR's cut; Jev adds +0.12 over the best model-free
control with far fewer regressions (3 vs 13). This agrees with the pilot
(+0.15 on 50 held-out queries with Gil + Gemini labels).

Operations: prep + Jev p50 0.46 s, p95 0.91 s; $0.35 per 1,000 searches;
fallback 2.3% (gate 1%): four 2 s timeouts in one two-hour window and four
client-side rejections where the score differed from the probability
expectation by more than 0.03 while both were otherwise valid.

## Decision

1. `search.jev_shadow.serve: true` makes the Jev call run inside the search
   and its order is returned to the caller: the same pool (served hits then
   the search's next-best pre-MMR candidates, up to `pool_size`), the same
   passage preparation, request and validation as shadow mode, truncated to
   the requested `limit`. No idle wait. The result list is replaced with
   `dataclasses.replace`; `degraded` and everything else in the outcome are
   unchanged.
2. Any failure returns the normal order: timeout, HTTP error, malformed
   response, budget stop, filtered query (`min_limit`, `skip_types`,
   sensitive queries, sampling) or an exception anywhere in the path.
3. Every run is still written to `_meta/jev-shadow.db` with a new `mode`
   column (`shadow` or `served`; existing ledgers are migrated in place).
   `served_ids` keeps holding the normal order and `jev_order` what Jev
   chose, so `scripts/jev_shadow_report.py` and the labelling procedure
   keep working unchanged on served traffic.
4. The default `timeout_seconds` rises from 2.0 to 3.0, and a score that
   disagrees with its probability distribution is logged at debug level and
   used rather than failing the batch. Both target the 2.3% fallback rate.

## Consequences

- Searches that reach Jev take about 0.5 s longer at p50 and under 1 s at
  p95 on brads-server; the ledger measures the live figure.
- The query log (`_log_results`) still records the pre-Jev order, since it
  is written inside the retrieval step. The ledger holds both orders.
- The candidate objects Jev reorders are the search's own `Result`s, so
  scores shown to the caller are the fused retrieval scores, not Jev's.
- Rollout: brads-server first (Ari vault), the shadow report after a few
  days, then other hosts with the usual notice. Rollback: `serve: false`
  (shadow continues) or `enabled: false`, then restart the daemon; MCP
  sessions pick the change up on restart.
