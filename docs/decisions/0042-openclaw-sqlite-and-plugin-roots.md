# ADR 0042: OpenClaw SQLite trajectories and approved plugin roots

Status: proposed implementation; production cutover requires operator approval.

OpenClaw 2026.9 stores rolling trajectory events in per-agent SQLite databases.
File-only ingestion misses them. Plugin skills may be symlinked into a workspace
from package installations. CLI discovery also mistakes a slow diagnostic health
response for an absent daemon.

## Decisions

- Native ingestion is opt-in per workspace via `layout.trajectory_sqlite_roots`.
  Each explicit state root discovers `agents/*/agent/openclaw-agent.sqlite`,
  including agents absent from the current runtime config (archived agents).
  No runtime config or source database is modified. Read transactions use
  URI `mode=ro` and `query_only`, include WAL, and close before yielding records.
- Poll database changes independently of file debounce and reconcile. Replay
  retained sessions on database/WAL change and on full reconcile. This deliberately
  uses session-level replay rather than trusting a high sequence cursor: resets,
  reused sequence numbers and retention can invalidate a cursor. No cursor can
  advance past an unsuccessful canonical write. Size limits skip with warnings.
- OpenClaw session identity is the existing `sessions/<session_id>.md` slot.
  Native, bridge and archived trajectory refs alias that same canonical ID.
  A monotonic merge of conversation turns retains prior history, including when
  the new source only contains a suffix or a disjoint post-reset window. Same
  session IDs across agents are OpenClaw's UUID identity contract; collisions
  across other source types fail rather than overwrite.
- Markdown remains canonical. No private transcript cache or unmanaged copies of
  skills are introduced. Bridge and archived JSONL remain readable; source
  disappearance never deletes session records. Events already pruned before any
  reader observed them cannot be recovered; sequence discontinuities are warned.
- Skill roots and approved symlink target roots are explicitly configured.
  Traverse only descendants of those roots, reject escaped symlinks, prevent
  cycles and canonicalize skill refs. Codex `.system` stays excluded.
- Daemon discovery requests lightweight identity from `/health?detail=false`,
  with a bounded compatibility timeout for old daemons. Full diagnostics retain
  their existing default. Discovery still verifies the exact resolved vault.

## Consequences

Replaying a changed database costs more than a trusted event cursor but is robust
against resets and restores with reused sequence numbers. Polling coalesces WAL
writes and parsing runs off the event loop. Monotonic transcript merging favors
retention over correcting or redacting old text; explicit vault deletion remains
an operator action. Full backups must include canonical session Markdown.
See `docs/openclaw-sqlite-migration.md` for parity, cutover and rollback.

## Cross-process query-log writes

Live CLI reproduction confirmed `SQLITE_BUSY` on query-log insertion. Ordinary
ingestion held the writer for several seconds; even a 15-second wait failed
under sustained writes. Increasing the timeout is insufficient.

Logging now tries a separate `BEGIN IMMEDIATE` transaction for 250 ms. On writer
contention it atomically persists the exact rows under `_meta/query-log-pending/`
instead of blocking search or discarding telemetry. The next successful search
or get log write replays up to 100 pending batches, committing before deleting
them. The queue is capped at 1,000 files / 16 MiB, each batch at 512 KiB; a full
queue or storage failure still warns. Replay is at-least-once across a crash
between commit and removal, appropriate for noncanonical retrieval telemetry.
No change to query-log privacy settings; disabled logging never enqueues rows.

The independent connection also avoids promoting a caller's stale read snapshot
or committing its transaction. `--no-daemon` remains independent of HTTP.
SQLite's [isolation rules](https://www.sqlite.org/isolation.html) explain the
read-to-write promotion constraint. The queue persists across process restarts;
older builds ignore it, so retain it until a fixed build can replay the rows.
