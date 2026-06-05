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

In the **reconcile path only** (`memstem.cli._reconcile_into_pipeline`),
skip records that are provably unchanged before calling
`Pipeline.process`. A record is "unchanged" when:

1. a record-map entry exists for its `(source, ref)` — i.e. we've stored
   it before, **and**
2. the normalized body hash of the incoming body still maps to that same
   memory id (`find_existing_memory_for_hash == lookup_record_mapping`).

For a skipped record we still call `Index.needs_reembed` and, if its
vectors are missing/stale (e.g. the embedder was down at first ingest),
`Index.enqueue_embed` — a cheap metadata check, no rewrite. Everything
else (new record, changed body, never-stored) falls through to the
normal `Pipeline.process`.

The live watcher path (`_drain_into_pipeline → Pipeline.process`) is
**unchanged**: real-time events are low-volume and must always apply
their full effect (frontmatter updates, transient TTL re-stamping, etc.).
This keeps the change isolated to the bulk path that actually has the
performance problem, and leaves the battle-tested ingestion path
untouched.

## Consequences

- A normal restart now reprocesses only what changed during downtime
  (usually a handful of records), so the reconcile finishes in seconds
  and the daemon is responsive immediately. Combined with the background
  reconcile + cooperative yielding from 0.12.1/0.12.2, the startup
  outage is eliminated.
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
