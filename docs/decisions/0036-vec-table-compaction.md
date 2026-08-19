# ADR 0036 — vec0 table compaction: reclaim dead vector slots

- **Status:** accepted
- **Date:** 2026-08-19
- **Relates to:** ADR 0030 (embed resilience), ADR 0032 (degraded search), ADR 0035 (search read connections)

## Context

`memories_vec` is a sqlite-vec `vec0` virtual table. vec0 (verified on
0.1.9) preallocates vectors in full 1024-slot chunks — 16 MB per chunk at
4096-dim float32, allocated in whole even for a single row — and frees a
chunk only when **every** slot in it is dead. Deleted slots are marked
invalid and *are* reused by later inserts, but a mass delete that leaves a
few survivors scattered in each chunk pins the whole table at its peak
size: no chunk ever empties completely, and steady-state insert volume
(thousands of rows/day against millions of holes) never refills the
overhang.

Mass deletes are routine here: `Index.upsert_vectors` replaces a record's
vectors with delete + reinsert, so any large source being re-chunked (a
multi-hundred-MB session trajectory shrinking under
`max_trajectory_bytes`), bulk purges, and slice rebuilds (ADR 0033) all
strand survivors across every chunk they once filled.

Production impact (brads-server main vault, 2026-08-19): 2,355,200 allocated
slots, 125,489 live rows — **5.3% occupancy**, a 36.8 GB vector table holding
~2 GB of live data. Every KNN query brute-force scans the whole table:

- warm (table in page cache): 10–20 s per search — long misread as the
  "normal corpus-bound latency envelope";
- cold (cache evicted; the table had outgrown its comfortable fit in RAM):
  tens of GB re-read from disk, searches stall 15–60+ minutes and complete
  together in a burst. This was the recurring "MemStem MCP degraded /
  timed out" incident (2026-07-01, 2026-08-09, twice on 2026-08-19).

## Decision

1. **`Index.compact_vectors()`** — stage all live `(chunk_id, memory_id,
   chunk_index, embedding)` rows into a plain table, drop the vec0 table,
   recreate it from its original DDL (dimensions preserved), reinsert, drop
   the stage. One transaction under the writer lock; row counts verified at
   both hops; failure rolls back. No re-embedding — the vectors are copied
   byte-for-byte.
2. **Hygiene stage `vec_compact`** (weekly by default) — runs the compaction
   when occupancy < `vec_compact_max_occupancy` (default 0.6) **and** dead
   slots ≥ `vec_compact_min_dead_slots` (default 25,000 ≈ 400 MB at
   4096-dim). The threshold gate lives inside the stage so healthy tables
   still advance `last_run`.
3. **CLI `memstem vec-compact [--vacuum] [--force]`** — the offline form for
   operators (daemon stopped). `--vacuum` additionally shrinks the db file;
   the in-daemon stage never vacuums (SQLite reuses freed pages internally,
   so the file stops growing either way).

## Consequences

- The in-daemon stage holds the writer lock for the duration of the rebuild
  (~2–3 min per 30k vectors at 4096 dims). Searches on pooled readers see
  the old table until the swap commits (WAL); writes queue. Weekly cadence
  and the dead-slot floor keep this rare and bounded.
- The transaction roughly doubles the live vector data in the WAL while it
  runs. Disk headroom of ~2× the live vector size is required.
- Does not change vec0's underlying allocation behavior; if a future
  sqlite-vec frees partially-empty chunks or defragments in place, the
  stage becomes a cheap no-op (thresholds never trip).

## Alternatives considered

- **Skip re-embedding unchanged chunks** — most churned records genuinely
  change (appended transcripts), so chunk contents shift and this saves
  little.
- **Upgrade sqlite-vec** — no released version with slot reuse at decision
  time; an upgrade is a separate, riskier change to the storage layer.
- **ANN index** — planned separately (`memstem-ann-prototype`); orthogonal.
  Compaction shrinks what any scan (brute-force or ANN build) must read.
