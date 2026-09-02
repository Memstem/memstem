# 0039 — Non-blocking vec compaction and event-loop hygiene

- Status: accepted
- Date: 2026-09-02
- Supplements: 0035 (search read connections), 0036 (vec table compaction)

## Context

On 2026-09-02 the weekly `vec_compact` hygiene stage (ADR 0036) ran for 2848 s
on a 147k-live / 360k-slot vault and **froze `/search` for the entire window**
(watchdog: 8 consecutive 60 s timeouts, 07:03–07:51 UTC). Two py-spy captures
during the incident showed the same stack: the asyncio **MainThread blocked in
`Index.claim_pending`**, called synchronously from `EmbedWorker.tick`.

Three separate defects compounded:

1. **`compact_vectors` holds `Index._lock` for the whole rebuild** — one
   transaction staging every live row out of the vec0 table, dropping it,
   recreating it, and reinserting. On a large vault that is tens of minutes.
2. **`EmbedWorker.tick` calls `claim_pending` on the event loop.** Every
   other index touch in the worker goes through `asyncio.to_thread`, but the
   claim didn't — so when the claim blocked on the compaction's lock, it took
   the entire event loop (HTTP server included) down with it. This is the
   direct freeze mechanism: ADR 0035 already moved search *retrieval* onto
   lock-free reader connections, and WAL means readers never wait on the
   write transaction — search would have survived the compaction if the loop
   had stayed alive.
3. **The query-log write at the end of each search blocks on `Index._lock`.**
   Even with (2) fixed, every search would still have hung at its final
   logging step for as long as a writer holds the lock. The log is explicitly
   non-canonical (see `retrieval_log.py`); losing rows under contention is
   fine, hanging a search to write them is not.

Separately, the weekly cadence lets dead slots accumulate to ~60 % of the
table (213k dead in one week on this vault), which every brute-force KNN scan
pays for all week (the ADR 0036 problem, re-created between compactions).

## Decision

1. **Event loop never blocks on the index lock.** `EmbedWorker.tick` claims
   pending rows via `asyncio.to_thread`, matching `_embed_one`.
2. **Search never blocks on the query log.** `log_search_results` / `log_get`
   acquire the serialization lock with a short timeout (default 2 s) and
   **skip the write** (debug log) when a long-running writer holds it.
   Retrieval-log rows are non-canonical by charter; importance seeding
   tolerates gaps.
3. **`compact_vectors` stages on a reader snapshot, not under the lock.**
   - *Phase A (lock-free bulk copy):* a borrowed reader connection full-scans
     `memories_vec` on its WAL snapshot; rows are appended to the plain
     staging table in small writer transactions (`batch_size` rows each, the
     lock held only per batch). Normal daemon writes interleave freely.
   - *Phase B (delta catch-up + swap, one short locked transaction):* rows
     staged for memories re-embedded since the snapshot opened
     (`embed_state.embedded_at >= t0`) or deleted meanwhile (memory gone
     from `memories`) are refreshed/dropped, then the vec0 table is dropped,
     recreated from its recorded DDL, and refilled from staging. Counts are
     verified at both hops; any mismatch rolls the whole swap back
     (ADR 0036's safety net is unchanged).
   - The locked window shrinks from "read 2.4 GB + rebuild" to "delta + vec0
     refill". During that window searches stay live: retrieval reads the old
     WAL snapshot (ADR 0035), the query log skips (2), and the event loop
     keeps serving (1). Embeds and distillation queue briefly — both are
     queue-and-retry by design.
   - `ALTER TABLE RENAME` on a build-aside vec0 table was rejected: verified
     empirically that sqlite-vec 0.1.9 does not rename its shadow tables
     (`no such table: main.<new>_rowids`), so drop-and-refill under the lock
     it is.
4. **Cadence: daily check, threshold-gated.** `vec_compact_interval_seconds`
   default drops 7 d → 1 d and `vec_compact_max_occupancy` rises 0.6 → 0.8:
   compact when the table is >20 % dead (and ≥ `min_dead_slots`, unchanged at
   25k). On this vault's churn (~30k dead slots/day) that lands roughly daily,
   keeps KNN scans near-minimal all week, and each run stays small. Vaults
   with low churn skip at the gate as before.

## Consequences

- A compaction can no longer take search down. Its cost becomes a brief
  write-queueing window instead of an outage.
- Query-log rows (and the importance signal derived from them) may be
  dropped for searches that land inside a long write transaction. Accepted:
  the log is bounded, lossy, and non-canonical by design.
- The staging table briefly duplicates live vector bytes on disk during
  compaction (as before — the stage simply lives longer now). `--vacuum`
  remains the CLI's opt-in for reclaiming file size.
- Concurrent re-embeds during Phase A are reconciled by `embedded_at`
  timestamp, which requires `embed_state` rows for live vectors (backfilled
  since ADR 0014). Rows without embed_state are treated as unchanged.
