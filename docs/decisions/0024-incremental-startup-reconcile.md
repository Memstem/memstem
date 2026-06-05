# ADR 0024 — Incremental startup reconcile

**Status:** Accepted (2026-06-05)
**Supersedes:** none
**Related:** ADR 0009 (queued embedding), ADR 0012 (exact-body dedup)

## Context

`memstem daemon` runs a startup *reconcile* — it re-walks every source
file (OpenClaw, Claude Code, Codex) and feeds each record through
`Pipeline.process` — to catch changes made while the daemon was down
(the live `watchdog` watchers only see events that occur *after* they
start).

The reconcile reprocesses **every** record on **every** restart, even
when nothing changed. `Pipeline.process` unconditionally re-writes the
canonical markdown file (`Vault.write`) and re-upserts the
memories/tags/links/FTS5 rows (`Index.upsert`). On the maintainer's
vault that is ~4,400 synchronous disk writes + SQLite upserts per
restart. Measured impact: the daemon spent **7–9 minutes** I/O-bound in
the reconcile before the HTTP/MCP server became responsive (0.12.0
blocked the bind entirely; 0.12.1/0.12.2 unblocked the bind but the
server was still starved by the write storm for minutes).

The catch-up *purpose* is necessary; reprocessing *unchanged* records is
pure waste — they already have identical markdown, index rows, and (when
the embedder was up) vectors.

## Decision

Three coordinated changes:

**1. `needs_reembed` reads `embed_state`, not `memories_vec`.**
`Pipeline.process` calls `needs_reembed` on every ingested record. It
began with `SELECT 1 FROM memories_vec WHERE memory_id = ?` to confirm a
vector exists — but that scans the sqlite-vec virtual table (~30ms/call;
no index on its id column), so it dominated both live ingestion and the
reconcile. A non-NULL `embed_state.body_hash` is written only *after* the
worker upserts the vector, so it is a faithful "has a vector" proxy —
verified 4,463/4,463 on the live vault (vector-present ⇔ embed_state
non-NULL, zero disagreements). `needs_reembed` now decides from the
`memory_id`-indexed `embed_state` row alone (~5µs).

**2. Reconcile skips unchanged records** (reconcile path only,
`_reconcile_into_pipeline`). A record is "unchanged" when a record-map
entry exists for its `(source, ref)` *and* `normalized_body_hash(body)`
still maps to that same memory id in `body_hash_index`. Both are ~5µs
indexed lookups, with no `memories_vec` scan.

The signal is `body_hash_index`, **not** `embed_state`, on purpose:
`body_hash_index` is written by `Pipeline.process` itself
(`record_body_hash`), so the skip converges after a single reconcile
**regardless of embedder health**. `embed_state.body_hash` is only
written *after* the embed worker succeeds — so while the embedder is
degraded (e.g. an OpenAI outage), an `embed_state`-keyed skip never
converges and every restart re-churns. `normalized_body_hash` also makes
the match whitespace-insensitive (the codex adapter, e.g., re-parses
with only a trailing-newline difference).

**3. Yield after every processed record.** `Pipeline.process` is
synchronous disk + index I/O and the adapters stream records without
awaiting, so the reconcile loop `await asyncio.sleep(0)` after each
*processed* record (and every 200 skips) to keep the HTTP/MCP server and
request handlers responsive while the background catch-up runs.

The live watcher path (`_drain_into_pipeline → Pipeline.process`) is
otherwise **unchanged**: real-time events must always apply their full
effect (frontmatter updates, transient TTL re-stamping). The change is
isolated to the bulk path that has the performance problem.

## Consequences

- Combined with the background reconcile + cooperative yielding from
  0.12.1/0.12.2, the startup outage is eliminated: measured on a staging
  copy of the live vault, the daemon's `/health` is responsive **~5s**
  after restart (was 7–9 min hard-unavailable) and stays responsive
  while the catch-up runs in the background.
- **Known limitation — pre-dedup / noise-filtered records don't
  converge.** A record ingested before the Layer-1 dedup table existed,
  or one the noise filter now drops on re-ingest, has no `body_hash_index`
  entry and `Pipeline.process` won't (re)write one for it, so the skip
  never matches it and it is re-evaluated every reconcile. This is cheap
  now (the ~30ms vec scan is gone) but not free. Fully eliminating it
  needs an **mtime-based incremental reconcile** — track the last-run
  time and skip source files whose mtime hasn't changed *before* parsing
  them, independent of any index-side hash. Tracked as a follow-up to
  this ADR.
- **Edge case:** a source file whose *frontmatter* changed during
  downtime without its *body* changing (e.g. a metadata-only edit, an
  importance re-seed) is skipped by the reconcile and won't pick up that
  frontmatter delta until its body next changes or a non-skipping path
  touches it. This is an accepted trade-off — body-identical records are
  the overwhelming majority on restart, and live edits still flow
  through the full `process` path.
- **Transient records:** an unchanged transient record is not re-stamped
  with a fresh `valid_to` on restart, so it expires on its original
  schedule rather than having its TTL extended — which is arguably more
  correct.
- Markdown remains canonical and the index remains fully rebuildable via
  `memstem reindex` (which does *not* use this skip — it always rewrites).
