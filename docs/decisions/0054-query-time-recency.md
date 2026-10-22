# 0054 — Query-time recency weighting, and a prefer-recent Jev prompt

- Status: proposed (both opt-in per vault; default off)
- Date: 2026-10-22
- Supersedes: the write-time decay slice of 0008 (never built)
- Builds on: 0008 (importance), 0047 (serve Jev's order)

## Context

Memory answers go stale: the embedding route, where reports are published,
which box runs a service. When an old record and a new one disagree, search
should lean toward the new one. Today almost nothing in ranking does:

- **Importance seed (0008).** At ingest a record gets up to +0.15 importance
  for being new, which the `1 + 0.2 × importance` boost turns into at most
  a ~3% score edge. The seed is computed once and never recomputed, and
  live capture means nearly every record is new when first seen, so a June
  session still carries a full "fresh" bonus today.
- **Decay (0008 Tier 3)** was specified but never built.
  `hygiene.decay_half_life_days` is read by nothing.
- **The importance stage** only raises importance on retrieval; it never
  lowers it.
- **Jev (0047)** is sent each document's `updated` date and the current
  time, but its instruction says "judge only this document's evidence", so
  it can't mark a document stale because a newer one contradicts it. Only
  queries that name a time get time-aware scoring.
- **Distillation** looks at a 120-day window of sessions to choose what to
  summarize; that affects coverage, not ranking.

Doing decay inside the hourly importance stage, as 0008 planned, would
rewrite the `importance` frontmatter of most of the vault's 23k files
again and again. It would also mix "useful" with "recent" in a single
stored number, so an old decision still in force would sink unless it
kept being retrieved.

## Decision

Two independent opt-in switches.

### 1. `search.recency` — a multiplier applied when the search runs

After `type_bias`, each materialized hit's score is multiplied by

    1 + weight × 2^(−age_days / half_life_days)

with `age_days` measured at search time from the record's **content date**:
`provenance.source_updated` when present (a distillation's original
session), else `updated`. Nothing is written to the vault or the index, so
turning it off restores the previous ranking exactly.

- `weight` 0.10 and `half_life_days` 30 by default: a record updated today
  gets +10%, one from a month ago +5%, one from six months ago +0.2%.
  That's enough to swap neighbours of similar relevance (the RRF gap between
  adjacent ranks near the top is 1–3%) without letting a weak fresh match
  jump a strong old one.
- `timeless_types` (skill, decision, person) and `pinned` records score as
  if updated today, so they never lose ground to fresh chatter.
- `skip_historical_queries`: a query that names an earlier time (a year, a
  month, "back in", "originally", "N months ago", "when did") is searched
  without the factor.
- Wired into the MCP, HTTP and direct-CLI search paths from
  `SearchConfig`. The eval harness can force it on or off.

### 2. `search.jev_shadow.prefer_recent` — a Jev prompt variant

When set, the per-document instruction becomes: judge this document's
evidence; if another document in the pool with a later `updated` date
contradicts it, it is stale and answers less well, unless the query asks
about an earlier time. The rest of the instruction (untrusted text,
proposals aren't actions, respect the requested time) is unchanged. Each
document's `updated` is sent as its content date (see above). Ledger rows
gain a `variant` column (`base` / `prefer-recent`; existing ledgers are
migrated in place), so `jev_shadow_report.py` and labelling can split the
two prompts.

### How the two interact

When Jev serves, its order replaces the fused order, so in production the
multiplier mainly decides **which records reach Jev's pool** (the served
hits plus the next-best pre-MMR candidates) and what is served whenever Jev
falls back (timeout, budget, filtered query: 2.3% at the 0047 trial, plus
`min_limit`/sensitive-query skips). The prompt variant is what changes
Jev's own judgement. Both are needed; neither alone covers the case.

## Evaluation (brads-server Ari vault, 2026-10-22)

`scripts/run_eval.py` gained `--production-ranking` (the vault's own
importance, type bias and MMR), `--recency/--no-recency`,
`--recency-weight`, `--recency-half-life` and `--jev off|base|prefer-recent`.
The index is opened read-only, and Jev runs log to a temporary ledger, never
the vault's. Jev shuffles documents with a time-based seed, so Jev rows
were repeated and are reported as a mean with the individual runs.

**Current-state set** (13 questions whose answer changed, kept in the vault's
`_meta/eval/` because they name private hosts and people — template in
`eval/recency_queries.example.yaml`; each expected phrase was checked to appear only in files updated
after the change):

| configuration | MRR | Recall@3 | Recall@10 |
|---|---|---|---|
| fused ranking, recency off | 0.340 | 0.38 | 0.69 |
| recency on (weight 0.10) | 0.342 | 0.38 | 0.69 |
| recency on (weight 0.20) | 0.342 | 0.38 | 0.69 |
| Jev, base prompt (production today), 3 runs | 0.448 (0.473 / 0.477 / 0.393) | 0.54 | 0.69 |
| Jev, prefer-recent, 3 runs | 0.495 (0.496 / 0.492 / 0.496) | 0.54 | 0.69 |
| recency 0.10 + Jev prefer-recent, 1 run | 0.487 | 0.54 | 0.69 |

Across runs the prefer-recent prompt ranked the current answer higher and
more consistently on "does memstem rerank…" (6–7 → 4–5) and "which
embedding route…" (5–9 → 4–5); the other found questions were the same or
within one rank. Four questions miss in every
configuration; those are retrieval misses that ordering can't fix.

**General set** (`eval/queries.yaml`, 16 queries): recency on changed MRR
0.845 → 0.843 without Jev. With Jev, base vs prefer-recent over four runs
each: 0.814 vs 0.817 mean MRR — the same within run-to-run noise
(0.79–0.85).

**Real traffic replay** (80 distinct MCP/HTTP queries from the query log
since 2026-10-08, fused ranking, recency 0.10 vs off): top-1 changed on
0 of 80, mean top-10 overlap 0.96 (minimum 0.70), median age of the top 5
went from 33.5 to 29.5 days; 6 of the 80 were detected as historical and
left alone.

Reading: the multiplier is safe but nearly inert on this vault. The
results above a stale-vs-current target are mostly not near-ties, and
much of the competition is itself recent, so a ≤10% edge rarely reorders.
It is still worth having for the Jev-fallback path and for pool
membership. Jev's prefer-recent prompt is the useful lever: about +0.05
MRR on current-state questions, steadier across runs, no change on the
general set. Both sets were hand-labelled by one agent, so treat them as
smoke tests; the 0047-style blind labelling of live traffic split by
`variant` is the real gate.

**Recommended rollout order:** `prefer_recent` first on brads-server, then
label a sample after a few days; turn the multiplier on afterwards if
wanted, since its effect is small either way.

## Consequences

- Off by default everywhere; behaviour is byte-identical until a vault sets
  `search.recency.enabled: true` or `search.jev_shadow.prefer_recent: true`.
- Searches with recency on materialize the wider re-score pool, as type
  bias already forces, so there's no extra cost on this host.
- Rollout, if adopted: brads-server first; check the Jev ledger split by
  `variant` after a few days, label a sample with the 0047 procedure, then
  other hosts with the usual notice. Rollback: set either flag back to
  `false` and restart the daemon; MCP sessions pick it up on restart.
- `hygiene.decay_half_life_days` stays in the schema for config
  compatibility and is documented as unused.
