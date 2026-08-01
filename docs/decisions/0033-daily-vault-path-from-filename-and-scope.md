# 0033 — Daily vault path derives from the filename date and source sub-directory

Status: **Accepted — implemented**
Date: 2026-07-25
Supersedes: none
Related: 0002 (markdown-canonical), 0005 (pull-based ingestion), 0026 (source-deletion tombstone)

## Context

Daily logs are the one record type whose vault path is *content-addressed by date*
rather than by id. `_path_for_memory` ([core/pipeline.py](../../src/memstem/core/pipeline.py))
assigned:

```python
if fm.type is MemoryType.DAILY:
    date = fm.created.date().isoformat()
    return Path(f"daily/{agent}/{date}.md")
```

That gives **one slot per (agent, date)**, and `fm.created` falls back to the source
file's **mtime in UTC** when the file carries no `created` frontmatter — which daily
logs written by an agent never do. Two failure modes follow, and both are live in
production:

**1. Off-by-one date shift.** An agent in America/New_York that appends to
`memory/2026-02-01.md` at 19:00 ET stamps an mtime of `2026-02-02T00:00Z`. The record
is written to `daily/<agent>/2026-02-02.md`, displacing the *actual* Feb 2 log, which
in turn displaces Feb 3, cascading through the run. In the production Ari vault, 31 of
182 top-level daily files carried an mtime hour of 21:00–23:00 UTC; `daily/ari/2026-02-02.md`
was verified to hold the body of `/home/ubuntu/ari/memory/2026-02-01.md`.

**2. Cross-directory collision.** `YYYY-MM-DD.md` is not unique within a workspace.
Ari's workspace also emits dated files under `memory/dreaming/rem/`,
`memory/dreaming/light/`, `memory/dreaming/deep/` and `memory/voice-reviews/`. All of
them target the same `daily/<agent>/<date>.md` slot and the last writer wins —
`daily/ari/2026-04-15.md` held a dreaming artifact, not the day's journal.

Measured impact on the production Ari vault before the fix: **250 dated source files
collapsed into 159 vault slots**, and the bodies of **39 of Ari's 182 daily journal
files appeared nowhere in the vault** — unrecoverable through search even though the
source markdown was intact on disk. Surviving entries were largely shifted by one day,
so date-scoped recall returned the wrong day's log.

The date in the *filename* is authoritative and stable; mtime is neither. And the
sub-directory a dated file lives in is exactly the disambiguator the slot was missing.

## Decision

The vault path for a `daily` record is built from adapter-supplied metadata, not from
`fm.created`:

```
daily/[<agent>/][<scope>/]<date>.md
```

- **`<date>`** comes from `record.metadata["daily_date"]` — the source filename stem,
  which the OpenClaw adapter already validated against `^\d{4}-\d{2}-\d{2}\.md$` when
  it classified the record as `daily`. Falls back to `fm.created.date()` when absent,
  preserving behavior for any adapter that does not supply it.
- **`<scope>`** comes from `record.metadata["daily_scope"]` — the source file's parent
  directory *relative to the configured memory dir it was found under*. A file sitting
  directly in `memory/` has no scope, so **the main journal keeps its existing
  `daily/<agent>/<date>.md` path**; `memory/dreaming/rem/2026-04-15.md` becomes
  `daily/<agent>/dreaming/rem/2026-04-15.md`.

Scope is computed by the adapter (which knows the workspace layout), not inferred in
`core/` by stripping path segments — per the adapter-discipline convention, per-AI
layout knowledge stays in `adapters/`.

Both components are validated at the pipeline boundary before they reach the path:
`daily_date` must match `^\d{4}-\d{2}-\d{2}$`, and each `daily_scope` segment must be a
plain relative name (no `.`/`..`, no separators, no leading `_`, per the reserved-dir
rules in `core/storage.py`). Anything else is discarded and the record falls back to
the derived date with no scope — a malformed adapter must not be able to write outside
`daily/`.

## Consequences

- Dated files from different sub-directories no longer overwrite each other, and a
  daily log lands on the date its filename claims regardless of the agent's timezone or
  when the file was last touched.
- `_path_for_memory` is only consulted for records with no existing path
  (`pipeline.process` reuses `existing.path`), so already-ingested daily memories keep
  their wrong path until relocated. `scripts/migrate_daily_paths.py` performs that
  one-shot relocation: it recomputes the correct path for every `daily` record from its
  `record_map` ref (or `provenance.ref` in frontmatter), moves the vault file, and
  updates `memories.path` in place — keeping the memory id, so links, embeddings and
  distillation references survive. Records whose recomputed path is already occupied by
  a different id are reported and left untouched.
- Freed slots are refilled by the next reconcile, which re-ingests the source files that
  had previously lost their slot.
- No re-embedding: bodies are unchanged, only paths move.
- Skill and session paths are untouched.
