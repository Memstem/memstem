# 0048 — Deadline on the semantic leg of a search

- Status: accepted (default on, 15 s; `search.semantic_timeout_seconds: null` disables)
- Date: 2026-10-03
- Builds on: 0032 (degraded flag), 0035 (borrowed read-only connections)

## Context

The vector half of a search is a brute-force vec0 scan over every stored
chunk (3.96 GB on the Ari vault). It answers in ~1 s while those pages are in
the page cache. When something else on the host evicts them, the next scan
re-reads the table from disk and can run for minutes. The Ari search watchdog
(one real search every 5 minutes) recorded this on brads-server: of 1,645
probes between 2026-09-27 and 2026-10-03, 16 took longer than 10 s; 3 came
back with results (13.5, 18.5, 47.7 s) and 13 never answered within the
60 s client timeout (five stall episodes). py-spy captures during each episode
show the request parked in `_query_vec_rows` with the query embedding already
returned in under 0.1 s. `sar` for the 2026-10-02 episode shows page reclaim of
20–69k pages/s and the page cache falling from 39 GB to 17 GB.

The embedder already had a timeout and a keyword-only fallback (ADR 0032),
but nothing bounded the scan itself, so a caller got nothing at all.

## Decision

Run the semantic leg — query embedding plus vector scan — in a small worker
pool (4 threads), on a borrowed read-only connection of its own, while BM25
runs on the caller's connection. Wait at most `semantic_timeout_seconds`
(default 15 s, Brad's choice after reviewing the numbers above). On overrun:

- return the BM25 results with `degraded=True` and
  `degraded_reason="semantic search exceeded 15s; keyword-only results"`,
  which already surfaces as `embedder_degraded` to MCP/HTTP callers;
- call `sqlite3.Connection.interrupt()` on the leg's connection. vec0 honours
  it (measured: a 150k×1024 scan stopped 0.06 s after the call). The
  interrupted connection is closed by `Index.reader()`, not re-pooled;
- interrupt only while the scan is running, under the leg's lock, so it can
  never land on a connection that has returned to the pool. The shared
  locked connection (fallback when no reader is available) is never
  interrupted because ingest writes run on it;
- a leg still queued, or still embedding, when its deadline passes does not
  start the scan.

15 s sits just above the sidecar's 14 s hedge deadline, so a slow but
successful query embedding still completes, and it would have kept one of the
three late successes above. The one-shot `memstem search` CLI keeps the
unbounded inline path.

## Consequences

- A cold-cache stall costs a caller 15 s and keyword-only results instead of
  60 s+ and an error.
- Two read-only connections per search instead of one; BM25 and the semantic
  leg now overlap, which slightly lowers normal latency.
- This bounds the symptom. Shrinking the scan (int8/binary quantized vectors
  with float rescoring) remains the durable fix on the roadmap.
