# 0040 — Atomic vec0 swap via shadow-table rename

- Status: accepted
- Date: 2026-09-03
- Supersedes the Phase-B design of 0039; supplements 0036

## Context

ADR 0039 made the bulk copy of `compact_vectors` lock-free but kept the
final step — `DROP TABLE memories_vec`, recreate, `INSERT ... SELECT` every
live row back — inside one locked transaction. On the 149k-row ari vault that
refill held `Index._lock` for ~20 minutes (2026-09-03 08:04–08:24 UTC): search
froze, the watchdog logged four consecutive 60 s timeouts, and Mission
Control's heartbeat emailed "MemStem Main DOWN". Because 0039 also moved the
cadence to daily, that became a nightly outage. The locked window was
O(live rows) — cadence could not fix it, only the swap mechanism could.

0039 had rejected a build-aside table + `ALTER TABLE RENAME` because
sqlite-vec 0.1.9 does not rename its shadow tables (a bare rename leaves
`no such table: <new>_rowids`). That conclusion was incomplete: renaming the
**virtual table and every shadow table** (`_info`, `_chunks`, `_rowids`,
`_vector_chunks00`, `_metadatachunks00`, `_metadatatext00`,
`_metadatachunks01`) — virtual table first, then shadows — yields a fully
queryable table. Verified against sqlite-vec 0.1.9 in the daemon venv.

## Decision

`compact_vectors` builds the compacted copy **beside** the live table and
swaps it in atomically:

1. Create `memories_vec_new` from the live table's DDL (name substituted).
2. Copy live rows into it with the 0039 bulk-copy loop (reader snapshot,
   keyset pagination, one short writer transaction per batch). This is the
   long phase and holds no lock across batches.
3. In one short locked transaction: refresh rows re-embedded since the copy
   started (`embed_state.embedded_at >= t0`), prune rows whose live chunk
   vanished, verify `count(new) == count(live)`, `DROP TABLE memories_vec`,
   then `ALTER TABLE ... RENAME` every `memories_vec_new*` table to
   `memories_vec*`. Any failure rolls the transaction back — the old table
   stays live — and the build table is dropped.

The locked window is now the delta + a count + a drop + eight renames:
milliseconds, independent of vault size.

## Addendum (2026-09-03, same day) — keep O(table) work out of the lock

The first live run of this design still held the lock ~2.3 min on the 149k-row
vault (4 search timeouts). Two O(table-size) steps were inside the swap
transaction: `DROP TABLE memories_vec` (freeing a multi-GB table page by page)
and the prune `DELETE ... WHERE chunk_id NOT IN (SELECT chunk_id FROM
memories_vec)` (two vec0 full scans). Revised:

- The old table is **renamed aside** to `memories_vec_old*` inside the swap
  and dropped afterwards in its own transaction. Search is already on the new
  table when the drop runs.
- The prune diffs the two `_rowids` shadow tables (plain B-trees whose `id`
  column is the row's chunk_id) and point-deletes the few chunk_ids that
  vanished, instead of a vec0 `NOT IN` scan. This stays correct for orphan
  vec rows (a `memories`-table prune would make every compaction abort
  while one exists — orphans do occur transiently, see the
  "Orphan vec rows cleaned" path in `record_embed_state`).
- Per-step timings (`delta`, `prune`, `verify`, `rename`, `drop_old`) are
  logged so the locked window is measured, not assumed.
- The build loop pauses `batch_pause_seconds` (default 0.1 s) between
  ~16 MB batches; during the unpaused live run concurrent searches ran
  2–14 s (baseline 2–3 s) from I/O contention. Compaction gets slightly
  longer; it holds no lock, so that is free.

## Addendum 2 (2026-09-03) — the last 48 s: secure_delete

Second live run: swap transaction 12 s (delta 11.9 s, prune/verify/rename
< 0.1 s) and it did **not** stall search; the one remaining search timeout
mapped exactly onto `drop_old` = 48 s. Ubuntu's SQLite is compiled with
`SQLITE_SECURE_DELETE`, so dropping the 2.4 GB old table zero-fills every
freed page through the WAL — the 48 s is that write, and its I/O starves
concurrent KNN scans. Scratch-table drops now run with
`PRAGMA secure_delete = OFF` (restored afterwards); the index is derived
data. The delta pass resolves affected chunk_ids from the `_rowids` shadow
tables and touches vec0 by primary key only (was a full-scan
`WHERE memory_id IN`). Build batches are 500 rows with a 0.25 s pause to
cut the write rate under concurrent searches.

## Consequences

- Compaction no longer degrades search at any vault size; daily cadence
  (0039) is safe again and the 2026-09-03 weekly stopgap override is removed.
- Shadow-table names are an sqlite-vec implementation detail. The rename
  step enumerates them from `sqlite_master` (prefix match) rather than
  hardcoding, so a sqlite-vec version that adds shadows still swaps cleanly;
  one that changes the naming scheme would fail the post-swap count check
  and roll back rather than corrupt.
- Peak disk during a compaction is live + built vectors (as before with the
  staging table); `--vacuum` remains the CLI opt-in to reclaim file size.
