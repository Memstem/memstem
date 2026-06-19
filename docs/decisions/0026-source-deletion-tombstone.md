# 0026 — Source-deletion tombstone (exclude memories whose authored source file was deleted)

Status: **Accepted — Phase 1 implemented (v2, revised after red-team)**
Date: 2026-06-18
Supersedes: none
Related: 0002 (markdown-canonical), 0005 (pull-based ingestion), 0011 (noise filter / `valid_to`), 0012 (LLM-judge dedup / `deprecated_by`), 0024 (incremental reconcile)

> **v2 changelog (red-team fixes):** authored-vs-derived is now decided by
> `memories.type` via a join, **not** by ref shape (the v1 premise that session refs
> "aren't file-backed" was false — every adapter sets `ref=str(path)`); `daily` logs are
> now in scope; dead `record_map` rows are deleted on confirmed-missing; the safety valve
> is now **per source-root**; exact-body duplicates now record a `record_map` row;
> `deleted_at` is added to the `upsert` column set; citations corrected.

## Context

MemStem is pull-based. Each source file in an agent workspace flows: **source file →
adapter emits `MemoryRecord{source, ref, …}` → pipeline writes a _separate_ canonical
copy into `~/memstem-vault/` at a derived path → SQLite index row.** Crucially, the
adapter's `ref` is the **on-disk source path** for every file-backed record —
`ref=str(path)` for memories, skills, daily logs, *and* session `.jsonl` files
(`claude_code.py`, `openclaw.py`, `codex.py`). The bridge from upstream source to memory
is the `record_map(source, ref, memory_id)` table, **defined** in
`_ensure_record_map` ([core/pipeline.py](../../src/memstem/core/pipeline.py)) and read via
`Index.lookup_record_mapping` ([core/index.py](../../src/memstem/core/index.py)).

**The gap.** When a user deletes a memory/skill/daily file they no longer want, they
delete it where they authored it. MemStem never notices:

- The watchdog handlers listen only for `on_created` / `on_modified` / `on_moved`.
  There is **no `on_deleted` handler** in any adapter.
- `reconcile()` only iterates files that _exist_, so a vanished source is never revisited.
- The one existing prune — `_prune_deleted_vault_files`
  ([src/memstem/cli.py](../../src/memstem/cli.py)) — checks whether MemStem's **own vault
  copy** is gone, not the upstream source. Because the vault holds a _separate_ copy,
  deleting your local file leaves the vault copy intact and it keeps appearing in search
  forever.

**Goal:** when an **authored** source file (`memory`, `skill`, or `daily`) is deleted
locally, label its memory so it is excluded from search by default, while remaining
auditable and recoverable. **Out of scope:** session logs (`.jsonl`, `type=session`) and
MemStem-generated records (`distillation`, `project`) — deleting/rotating those must never
hide them or the insights derived from them.

## Decision

### 1. Marker: a dedicated `deleted_at` tombstone (soft-delete, recoverable)

Add an optional `deleted_at: datetime | None` field to `Frontmatter`
([core/frontmatter.py](../../src/memstem/core/frontmatter.py)), and a nullable
`deleted_at` column to the `memories` table.

**Schema change:** bump `SCHEMA_VERSION` to 14 with a marker `MIGRATIONS[14]` entry
(`SELECT 1;`), and add the column via a PRAGMA-gated `_ensure_deleted_at_column()` helper
called from `_migrate()` — the same pattern as `_ensure_embed_queue_claim_columns`. This is
required because `ALTER TABLE ADD COLUMN` has no `IF NOT EXISTS` form and the migration
scripts must stay replay-safe (the legacy-upgrade path re-runs them against an
already-current schema), so the ALTER cannot live inside a migration script.

**`upsert` must carry the new column.** `Index.upsert` / `_memory_params`
([core/index.py](../../src/memstem/core/index.py)) currently write a fixed column list
(`… valid_to, embedding_version, deprecated_by`) with no `deleted_at`. Add `deleted_at` to
the INSERT column list, the `ON CONFLICT DO UPDATE SET` clause, and `_memory_params`, or
the tombstone will never persist to the index.

**Search filtering.** `Search.search()` ([core/search.py](../../src/memstem/core/search.py))
gains an `include_deleted: bool = False` parameter; the private `_materialize()` filter
(alongside the existing `valid_to`/`deprecated_by` checks) skips records with `deleted_at`
set unless `include_deleted=True`. Mirrors `include_expired` / `include_deprecated`
exactly.

**Why a new column, not `valid_to=now`:** `_build_frontmatter`
([core/pipeline.py](../../src/memstem/core/pipeline.py)) lets a fresh transient tag
**overwrite** `valid_to` on re-ingest, and the noise filter owns `valid_to` semantics.
Encoding deletion there would let a re-ingest clobber the deletion marker and would make
"un-expire" and "un-delete" indistinguishable. A separate column avoids the collision. Cost
is one nullable column.

### 2. Detection: reconcile-driven source-liveness sweep (authoritative)

After each reconcile runs the adapters, and **before** `_prune_deleted_vault_files`, run a
source-liveness sweep:

1. Enumerate source mappings **joined to type** via a new lock-holding index method
   (see §3): `(source, ref, memory_id, type)`, grouped by `memory_id`. (`record_map` has
   no type column, so the join to `memories.type` is mandatory — this is the real
   authored-vs-derived guard.)
2. Consider only memories whose `type ∈ {memory, skill, daily}`. `session` is excluded by
   type (even though its ref is a real file), which is what protects distillations and
   raw logs. Rows whose `memory_id` has **no** `memories` row (orphaned by a prior
   vault-prune) are skipped and their `record_map` row is deleted.
3. For each remaining `ref`, ask the owning adapter `source_exists(ref)` (§3). When a ref
   is confirmed **missing**, **delete that `record_map` row** in the same locked
   transaction (this keeps the table from accumulating dead refs and makes "all refs
   dead" actually computable — see §5/C-fixes).
4. **Tombstone a memory only when it has no surviving `record_map` row.** Re-`stat()`
   immediately before writing (guards the read-decide-write race against a concurrent
   restore/re-ingest), then set `deleted_at = now` on the vault file **through
   `core/storage.py`** and `index.upsert` it (never the index directly — storage
   invariant).

**Clear-on-restore is automatic.** If a deleted file comes back, the adapter re-emits it;
`_build_frontmatter` does **not** preserve `deleted_at` (unlike `valid_to`/`deprecated_by`),
so the normal ingest path writes the record with `deleted_at` cleared and re-creates its
`record_map` row. No separate clearing logic needed; the re-`stat()` in step 4 prevents a
stale sweep from re-tombstoning a just-restored file.

Reconcile-driven (not event-driven) is v1 because a `stat()` at reconcile time reflects
_final_ state, sidestepping atomic-save churn (write-temp → rename-over fires spurious
delete/create). Latency = reconcile interval, acceptable for "I deleted a note."

### 3. Liveness check + enumeration API

Add a thin, adapter-owned liveness hook to the `Adapter` base
([adapters/base.py](../../src/memstem/adapters/base.py)):

```python
def source_exists(self, ref: str) -> bool:
    """Does the upstream source for this ref still exist on disk?
    For these adapters every authored ref is a path: Path(ref).is_file().
    """
    return Path(ref).is_file()
```

The **type allowlist in §2.2 is the primary derived-guard**, not `source_exists` — the
hook only answers liveness. It lives on the adapter per CLAUDE.md adapter discipline (only
the adapter knows what a `ref` means), so a future non-path ref can override it.

Add a lock-holding `Index.all_source_mappings() -> list[tuple[str, str, str, str]]`
(`source, ref, memory_id, type`) joining `record_map → memories`, plus a locked
`Index.delete_record_mapping(source, ref)`. A bare `db.execute` from the reconcile daemon
thread while embed workers share the connection trips `SQLITE_MISUSE`; every cross-thread
access must hold `self._lock` (mirrors `all_paths`, `lookup_record_mapping`).

### 4. Exact-body duplicates must record a ref

Today a cross-record duplicate returns from `Pipeline.process`
([core/pipeline.py](../../src/memstem/core/pipeline.py)) **before** `_record_mapping`
runs, so a deduped duplicate file is never tracked. Consequence: if files A and B have
identical bodies (B deduped onto A's `memory_id`) and the user deletes the **canonical** A,
the only tracked ref dies → the memory is tombstoned **even though identical content B
still exists on disk**, and B keeps re-deduping against the tombstone so it never
self-heals.

**Fix:** in the duplicate branch, write `record_map(source, ref) → existing_id_for_hash`
before returning. The new row points at the *same* canonical id (it doesn't "fight" the
canonical entry — `(source, ref)` is the PK), so all contributing sources are tracked and
"all refs dead" is correct.

### 5. Safety valve — per source-root, not global

A missing mount is a **per-root, all-or-nothing** signal, so the valve must be evaluated
per adapter source-root (workspace), not over a global `total_authored`:

- If a single root has **~100%** (configurable, default ≥ `max(10, max_fraction)` of that
  root's authored refs) go dead in one sweep → treat as a vanished/unmounted root → **skip
  that root with a loud error**, tombstone nothing under it.
- Partial deletions within a present root are real deletions → proceed.

This avoids both v1 failure directions: wrongly blocking a legitimate one-workspace cleanup
in a small vault, and wrongly mass-tombstoning a dropped workspace inside a large
multi-agent vault. An explicit operator action (`memstem reindex` / `--force`) may override.

### 6. Sweep ordering vs the existing vault prune

Run the **source-liveness sweep first** (it tombstones rows that still have vault files),
**then** `_prune_deleted_vault_files`. If a tombstoned record's vault copy is later removed,
the existing prune hard-deletes the `memories` row (cascading FTS/vec/embed); the
source-liveness sweep's orphan handling (§2.2) then drops the now-dangling `record_map`
row on its next pass. The two mechanisms stay orthogonal: source-deletion → soft tombstone
(vault copy kept); vault-copy-deletion → hard prune.

## Edge cases

1. **Atomic save** (temp + rename): reconcile `stat()` sees the final file → no false
   tombstone. Phase-2 `on_deleted` must debounce + re-stat.
2. **Move / rename:** the new path ingests under a new ref; the old ref's row is confirmed
   dead and **deleted** (§2.3). If frontmatter carries a stable `id`, both refs resolve to
   one `memory_id` and the live new ref keeps it un-tombstoned. If not, ingestion assigns a
   fresh id via `record_map` (not a path-derived uuid5 — see Corrections), so the moved
   note becomes a new memory and the old one tombstones once its dead row is gone. Dedup
   judge (0012) reconciles the pair.
3. **Multi-ref / duplicate:** §4 ensures every contributing source has a ref; tombstone
   only when all are dead.
4. **Mass disappearance / unmounted root:** §5 per-root valve.
5. **Restore:** automatic via re-ingest (§2 clear-on-restore).
6. **MCP-born / `memstem_upsert` records:** their ref isn't an upstream authored file;
   `source_exists` for the owning source returns appropriately, and their lifecycle stays
   with the vault-file prune. (Confirm the MCP source's `source_exists` doesn't false-stat
   a non-path ref before shipping.)
7. **FTS/vec rows:** tombstoned records stay indexed and are filtered at materialize time
   (same as `valid_to`/`deprecated_by`) — no re-embed.

## Corrections to v1 (grounded against the code)

- The public search method is `Search.search()` (not `query()`); the default filtering is
  in the private `_materialize()`.
- `_prune_deleted_vault_files` lives in `src/memstem/cli.py` (not `core/cli.py`).
- `record_map` is **defined** in `core/pipeline.py` (`_ensure_record_map`); `core/index.py`
  only reads it.
- v1's "move → `coerce()` derives a new id from the path" was wrong: during ingestion id
  identity is anchored by `record_map`, and `coerce()` is called with **no path** (id
  already set). The uuid5-from-path branch only fires in `Vault.read` of a vault file
  missing its id, and it uses the **vault** path. Edge-case #2 is rewritten accordingly.
- Column adds use a one-shot `MIGRATIONS[n]` entry (or the PRAGMA-gated `_ensure_*_columns`
  helper) — `deprecated_by` has existed since `MIGRATIONS[1]`, so it is not an
  add-column precedent.

## Alternatives considered

- **Reuse `valid_to = now`** — rejected: collides with the noise filter's `valid_to`
  overwrite-on-re-ingest and conflates expiry with deletion (see §1).
- **Event-only (`on_deleted`)** — rejected as primary: fragile against atomic-save churn
  and dropped inotify events, no self-heal. Viable Phase-2 accelerator.
- **Hard-delete on source removal** — rejected: not recoverable; maintainer asked for a
  label. A later TTL hard-pruning long-tombstoned records is possible, out of scope.

## Phasing

- **Phase 1 (this ADR):** `deleted_at` field + `MIGRATIONS[14]` + `upsert`/`_memory_params`
  column; `_materialize` filter + `include_deleted` on `Search.search()`. (Caller exposure
  matches the existing convention: `include_expired`/`include_deprecated` are internal-only
  with default `False` and not surfaced through MCP/HTTP/CLI; `include_deleted` follows
  suit, so deletion-exclusion is automatic and surfacing tombstones is a deferred audit
  capability rather than an inconsistent new flag.) `Adapter.source_exists`;
  `Index.all_source_mappings` + `delete_record_mapping`; duplicate-ref recording (§4); the
  reconcile sweep with per-root valve + re-stat; orphan-row cleanup; ordering before the
  vault prune. Tests.
- **Phase 2 (later):** `on_deleted` watchdog accelerator that re-stats before tombstoning.

## Testing

- `source_exists` per adapter (file present/absent).
- Sweep: tombstone when sole ref dead; **no** tombstone when a sibling ref alive; deletes
  dead `record_map` rows; clears via re-ingest on restore; orphan-row cleanup.
- Type guard: deleting a `session` `.jsonl` does **not** tombstone it or its distillation.
- Daily: deleting a `daily` `.md` **does** tombstone it.
- Duplicate: identical A+B, delete canonical A → not tombstoned while B exists (§4).
- Safety valve: a vanished root is skipped; a partial in-root cleanup proceeds.
- Search: `deleted_at` excluded by default, surfaced with `include_deleted=True`, across
  MCP/HTTP/CLI.
- Migration: column adds once; existing rows default `NULL`; `upsert` round-trips the field.
- Concurrency: re-ingest mid-sweep does not produce a false tombstone (re-stat guard).

## Out of scope

- Cascade tombstoning of distillations/project records derived from a deleted source.
- Hard pruning of tombstoned records (possible later TTL).
- A UI/MCP tool to delete a memory directly.

## Open decision for the maintainer

`daily` logs are **included** as authored/deletable (recommended — it's the most common
deletion). Say so if you'd rather scope daily logs out.
