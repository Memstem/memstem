# 0035 — Search reads on dedicated read-only connections; bulk ingest yields to searches

Status: **Accepted — implemented**
Date: 2026-08-15
Supersedes: none (refines the single-connection policy documented on `Index`)
Related: 0024 (reconcile skip-unchanged), issue #142 (reconcile starved search)

## Context

The daemon holds **one** SQLite connection guarded by one `threading.RLock`
(`Index._lock`). Every index touch — ingestion upserts, embed-vector writes,
and search reads — serializes through it. The docstring justified this as
"far less complex than a per-thread connection pool," and for a quiet vault
it is.

It fails under bulk ingest. On 2026-08-15, adding a new session directory to
a production vault's openclaw adapter triggered a startup backfill (months of
trajectories, including one 151 MB file). For ~28 minutes every search — MCP
and CLI — timed out at 60–120 s while reconcile held the lock in back-to-back
upserts. Issue #142 had already moved `pipeline.process` off the event loop
into a worker thread, which kept the *loop* responsive but not the *lock*: a
search still had to interleave its many locked reads between thousands of
rapid-fire writer acquisitions.

The irony: `connect()` already sets `PRAGMA journal_mode = WAL` — a mode
whose whole point is readers running concurrently with a writer — and then
the application layer defeats it by funneling readers through the writer's
connection.

Searches are latency-sensitive (an interactive agent is mid-turn, a voice
caller is on the line). Ingestion is not — it is queued background work.
The priority is therefore fixed: **searches must never queue behind bulk
ingest.**

## Decision

Three coordinated changes:

1. **Search reads run on dedicated read-only connections.**
   `Index.reader()` lends a connection opened with `mode=ro` +
   `PRAGMA query_only`, sqlite-vec loaded, from a small reuse pool
   (`READER_POOL_MAX = 4`). WAL gives it a consistent snapshot concurrent
   with the writer; it never takes `Index._lock`. The read methods a search
   needs (`query_fts`, `query_vec`, `get_path`, first-chunk embedding
   lookups) accept an optional `db=` connection. One search holds one reader
   for its whole lifetime, so a pooled connection is never used by two
   threads at once — the cross-thread hazards documented on `get_path`
   (shared statement cache → `SQLITE_MISUSE`) don't apply. Cache and log
   *writes* on the search path (HyDE cache, rerank cache, query log) stay on
   the shared locked connection.

   `reader()` yields `None` when a read-only connection can't be opened
   (index file not yet created, exotic filesystem) and every caller falls
   back to the pre-0035 locked path — behavior-compatible degradation.

2. **Bulk ingest yields to in-flight searches.** `Search.search_with_status`
   wraps itself in `Index.search_started()/search_finished()`; the reconcile
   loop calls `_yield_to_searches()` before each record it processes,
   sleeping in 0.1 s ticks while `searches_in_flight > 0`, bounded at 10 s
   per record so ingest always progresses. With readers in place this is not
   about the lock — it stops reconcile's Python-side work from time-slicing
   against a search's scoring on the GIL.

3. **Oversized trajectories are skipped at ingest.**
   `OpenClawLayout.max_trajectory_bytes` (default 64 MiB, `0` = unlimited)
   caps what `_trajectory_to_record` will read; over-cap files log a warning
   naming the file and the remedy (truncate/archive or raise the cap). A
   single runaway session log — the 151 MB file above was one stuck voice
   session — can no longer monopolize the daemon for its parse duration.

## Consequences

- Search latency during reconcile drops from lock-queue-bound (observed:
  60–120 s timeouts) to its normal profile; the remaining interference is
  GIL scheduling, which (2) bounds.
- The writer's throughput is unaffected when no search runs; during a
  search, reconcile pauses up to 10 s per record — acceptable for queued
  background work by the priority stated above.
- Readers see a WAL snapshot, so a search may miss a record committed
  mid-flight. That staleness window (milliseconds-to-seconds) is already
  inherent to search-vs-ingest timing and was never guaranteed otherwise.
- Anything still using the shared connection (writes, hygiene, `/health`)
  keeps the existing lock discipline — this ADR narrows the lock's scope,
  it does not remove it.
- The layout cap is enforced in `_trajectory_to_record`, the single choke
  point both reconcile and the watch loop route through.
