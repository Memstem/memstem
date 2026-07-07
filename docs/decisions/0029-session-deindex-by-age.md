# 0029 — Session de-indexing by age (search-latency hygiene)

Status: **Proposed — NO-GO as written pending council-driven revision (see §Council review 2026-07-07)**
Date: 2026-07-07
Supersedes: none
Related: 0026 (source-deletion tombstone / `deleted_at`), 0020 (session distillation), 0023 (in-daemon hygiene loop), 0024 (incremental reconcile), 0002 (markdown-canonical)

## Context

Vector search is **brute-force**. `memories_vec` is a sqlite-vec `vec0` virtual table with
no ANN index, so every query does exact KNN over **all** vector chunks. Search latency is
therefore ∝ the number of indexed chunks, and grows without bound as vaults accumulate.

Measured on the ari vault (2026-07-07): 9,091 memories → **73,637 chunks → ~12–18 s/query**.
Breakdown by `type` (chunks = the scan cost):

| type | chunks | % of scan | retrieved in a 30-day window |
|---|---|---|---|
| **session** | 49,961 | **68%** | 6.7% of sessions |
| memory | 19,870 | 27% | 9.9% |
| daily / distillation / skill / project | ~3,800 | 5% | higher |

**Raw sessions dominate the scan cost (68%) yet have the lowest retrieval rate — 93% of
sessions never surfaced in a full month of queries.** They are the ingest layer; their
signal is preserved compactly elsewhere: coding sessions (claude-code/codex) get a
`type=distillation` companion (ADR 0020, ~1.7 chunks vs a session's ~9.5), and OpenClaw
sessions have their facts extracted into `memory`/`daily` records. Old raw sessions are
almost pure scan-cost overhead.

ADR 0026 added a recoverable `deleted_at` tombstone but **deliberately scoped sessions
out** ("deleting/rotating those must never hide them or the insights derived from them").
That was correct for *source-deletion*. This ADR addresses a *different* trigger — **age**,
for **latency** — and a different disposition: a session is not deleted, it is moved to a
cold, still-recoverable tier that no longer costs a vector scan.

Two facts from the code make the mechanism non-obvious:
1. `deleted_at` is filtered in `Search._materialize()` **after** the vec0 KNN scan
   ([core/search.py](../../src/memstem/core/search.py)). A tombstone alone hides a record
   from results but **still scans its vectors** → no latency benefit.
2. The ingest/embed path does not check `deleted_at`, so a tombstoned file that remains in
   the vault would be **re-embedded** on the next reconcile, resurrecting its vectors.

## Decision

Add an **age-triggered, reversible session de-index**: tombstone the session **and** strip
its vectors/FTS rows, keep the `memories` row and the canonical `.md` in the vault, and
teach the embed path to leave tombstoned records stripped.

### 1. De-index primitive — `Index.deindex(memory_id)` (new)

Distinct from `delete()` (which hard-drops the `memories` row). `deindex`:
- sets `memories.deleted_at = <now>` (reuses the ADR 0026 column — no schema bump),
- `DELETE FROM memories_vec / memories_fts / embed_queue / embed_state WHERE memory_id = ?`
  (the latency win: the chunks leave the scanned set),
- **keeps** the `memories` row (audit/recovery) and the `.md` file (canonical, still
  grep-able and openable for manual search).

### 2. Embed path skips tombstoned records (new guard)

In the enqueue/reconcile path ([core/pipeline.py](../../src/memstem/core/pipeline.py)):
a record with `deleted_at` set is **not** enqueued for embedding. This keeps a de-indexed,
vault-resident session stripped across reconciles, and is correct in general — there is no
reason to embed a record excluded from search. (ADR 0026 tombstones benefit too: no wasted
embed work on hidden records.)

### 3. Reverse — `Index.reindex_memory(memory_id)` (new)

Clear `deleted_at` and `enqueue_embed`. Because the `.md` and `memories` row never left,
recovery is a re-embed, not a re-ingest. Exposed via `memstem hygiene reindex-sessions --ids …`.

### 4. Hygiene command — `memstem hygiene deindex-sessions`

Follows the planner/applier + dry-run-default pattern of `distill-sessions`:
- Selects `type=session`, `deleted_at IS NULL`, `created < now − ttl_days`.
- **Preservation safety rail (default on).** De-index a session only if its content is
  preserved: it has a linked `type=distillation`, **or** it is below the meaningfulness
  threshold (the `distill-sessions` turn/word floor — trivial/noise), **or** its source
  facts are extracted (OpenClaw `memory`/`daily`). Sessions that are **meaningful,
  undistilled, and not otherwise preserved** are **skipped and reported** ("distill these
  first"); `--force` overrides.
- **Fraction safety valve** (ADR 0026 pattern): abort if a single run would de-index more
  than `--max-fraction` (default 0.5) of live sessions, unless `--force`.
- Flags: `--older-than-days` (default from config), `--apply` (default dry-run),
  `--force`, `--max-fraction`, `--vault`.

### 5. Config — `hygiene.session_index_ttl_days`

New key (default `30`; `0`/null disables). When set and the in-daemon hygiene loop
(ADR 0023) is enabled, the loop runs `deindex-sessions` on its cycle so the policy is
self-maintaining fleet-wide and session scan-cost is **bounded to ~one TTL window** instead
of growing unbounded.

## Consequences

- **Latency:** removes old-session chunks from every future scan. On ari, de-indexing
  sessions >30 days trims ~15k of ~74k chunks (~15 s → ~12–13 s immediately); the larger,
  durable win is bounding growth. This is complementary to — not a substitute for — an ANN
  index (a separate ADR), which removes the ∝-scan-size cost entirely.
- **Recoverability:** nothing is deleted. `.md` files remain canonical and manually
  searchable; `reindex-sessions` restores full search for any id; `include_deleted=True`
  still surfaces them via the API.
- **Safety:** the preservation rail means no session leaves the index without its content
  living on in a distillation, extracted memories, or being trivial; the fraction valve
  guards against a mis-scoped bulk run.
- **Non-goals:** does not delete `.md` files, does not touch `distillation`/`memory`/
  `project`/`skill` records, does not change ranking for live records.

## Alternatives considered

- **Archive `.md` out of the watched vault + `_prune_deleted_vault_files`.** Simple and
  reuses existing prune, but hard-drops the `memories` row (less auditable) and moves files
  out of the canonical tree; recovery is a re-ingest. Rejected in favor of the in-place,
  in-index tombstone which is more recoverable and keeps the vault whole.
- **Tombstone only (no vector strip).** Zero latency benefit (§Context fact 1). Rejected.

## Council review (2026-07-07, task #37 — panel: Claude+Codex+Grok, judge, adversarial critic, empirical verification leg)

**Verdict: NO-GO as written for the 15-vault fleet.** Motivation sound; mechanism unsafe as specified, and priority is questionable. Empirically VERIFIED against the code (branch feat/session-deindex-hygiene):
- Search filters `memory.frontmatter.deleted_at` via `vault.read` (search.py:527,539), NOT the DB column → a DB-only tombstone is invisible AND undone by rebuild.
- `memstem reindex` re-reads every `.md` and `enqueue_embed`s unconditionally (cli.py:684,696,698; default embed=True), no `deleted_at` check → it resurrects every de-indexed session. Verified, not inferred.
- `memories_fts` is a regular (non-contentless) FTS5 table → `DELETE FROM memories_fts WHERE memory_id=?` is valid as written (open gap CLOSED).

**Required revisions before this ADR can be Accepted:**
1. Use a DISTINCT lifecycle primitive (`deindexed_at` + `deindex_reason`, or `search_state`) — do NOT overload ADR-0026 `deleted_at`. Canonical in `.md` frontmatter (survives reindex) + mirrored to a DB column checked in-transaction.
2. Enforce the skip at EVERY vector/FTS write boundary via one `upsert_vectors_if_live()`/`should_index()` all writers route through (embed worker, reindex, pipeline, startup reconcile) — not just `enqueue_embed`. Guard the inverse too (never suppress a LIVE record).
3. Preservation = COVERAGE proof, fail-closed: linked distillation that exists + is non-stale (source-hash match, distilled after session close, current parser) with claim/source coverage; else KEEP INDEXED and report. Drop the meaningfulness-floor auto-exemption unless separately reviewed/sampled.
4. No auto-revive from a body-hash change (formatter/line-ending/sync flips it) — cold→live only via explicit operator/policy `reindex_memory --revive`.
5. Broadened reindex-resurrection test (vectors+FTS+queue+state+lifecycle+tags/links+search, before AND after daemon restart); concurrency integration test (watcher+hygiene+worker on one id); consistent-snapshot rollback drill (quiesce daemon + WAL checkpoint before snapshot); one-vault canary with caps by ABSOLUTE sessions/chunks/latency.
6. If shipped, prefer the cold-tier + manifest shape with a TWO-PHASE transition (manifest first → mark state → quiesce prune → move file → prune only if a valid manifest exists), so `_prune_deleted_vault_files` can't race the move and drop the row. Recovery is "logical revival," not byte-for-byte.

**Strategic (highest-value council finding):** the de-index buys only ~15–20% while brute-force scan remains; the ANN index (deferred here) is the asymptotic fix. But "do ANN first" is itself an unproven pivot. NEXT STEP recommended: a measured ANN prototype on one vault (recall@k + latency + ranking-regression vs brute-force) to decide the roadmap on data. Treat ANN and de-index as SEPARATE decisions.
