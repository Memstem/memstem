# ADR 0014: CLI daemon delegation + one-shot migration discipline

Date: 2026-04-29
Status: Accepted

## Context

Two related problems surfaced in production on the v0.7.0 vault, both
ultimately rooted in the same architectural assumption: that opening
the SQLite index is cheap enough to pay on every CLI invocation.

### Symptom

After Brad migrated the embedder from Gemini 768-dim to OpenAI
3072-dim, `memstem search "Kinsta"` from the shell began timing out at
30 s. The MCP tool surface and the daemon's HTTP `/search` endpoint
returned in milliseconds. Same query, same code, same vault — the
difference was per-process lifecycle, not query complexity.

### Root cause 1 — backfill on every connect

`Index._backfill_embed_state()` was historically called from
`Index._migrate()` on **every** `connect()`, after the migration loop:

```python
for version in sorted(MIGRATIONS):
    ...  # apply pending migrations
self._ensure_vec_table()
self._backfill_embed_state()  # runs every time, even when no-op
self.db.commit()
```

The function was *intended* to be idempotent (the comment says
"subsequent boots are a no-op"), but the SELECT it issues to detect
"nothing to backfill" is itself the problem:

```sql
SELECT m.id, m.body
FROM memories m
WHERE EXISTS (SELECT 1 FROM memories_vec v WHERE v.memory_id = m.id)
  AND NOT EXISTS (SELECT 1 FROM embed_state s WHERE s.memory_id = m.id)
```

`memories_vec` is a `vec0` virtual table without an auxiliary index on
`memory_id`. SQLite's plan for the EXISTS subquery is a full scan of
the vec0 table, **per row in `memories`**. With 1,211 memories ×
12,296 chunks × 3072-dim float vectors (≈ 1.1 GB on disk), that's ~35
seconds of CPU on every connection — even when there is literally
nothing to backfill.

The MCP server and HTTP server are inside `memstem daemon`, a long-
running process. They pay this cost once at startup. The CLI opens a
fresh connection per invocation and pays it every time.

### Root cause 2 — CLI as a full library client

Even with the backfill fixed, every `memstem search` still pays:

- `sqlite3.connect()` + WAL setup
- `sqlite_vec.load()` (extension load + symbol resolution)
- `_migrate()` scan
- `httpx.Client` construction for the embedder
- An embedding API round-trip for the query string

These are individually small but they don't need to happen at all
when a daemon is already running on loopback with the connection hot,
the embedder warm, and the index pages cached.

The current shape — CLI opens the database directly — is the
straightforward thing to build first, but it scales every per-process
startup cost across every CLI call. As the vault grows and the
embedder changes, the gap between "MCP/HTTP search is instant" and
"CLI search hangs" widens. Backfill was the first symptom; without an
architectural fix the next one is queued behind it.

## Goals

1. CLI search returns in tens of milliseconds when the daemon is
   running, regardless of vault size.
2. CLI works correctly when the daemon is *not* running, with no
   user-visible difference except latency.
3. Migration backfills run once per upgrade, not once per connection.
4. The fix generalizes — future migrations that need backfill steps
   are encoded in the schema-version model, not patched onto
   `connect()`.

## Decision

Three changes, landed as three sequential PRs.

### Decision 1 — Backfill runs at most once, gated on `schema_version`

`Index._migrate()` captures the schema version *before* applying
migrations. After the migration loop and `_ensure_vec_table()`, the
backfill runs only when crossing the v8 boundary:

```python
def _migrate(self) -> None:
    old_current = self._read_current_version()  # 0 for fresh installs

    for version in sorted(MIGRATIONS):
        if version <= old_current:
            continue
        self.db.executescript(MIGRATIONS[version])
        ...

    self._ensure_vec_table()
    if old_current < 8:
        # Fresh installs hit this via the v8 marker migration above.
        # Legacy installs (v3..v7) hit it once on first open after
        # upgrade, then schema_version == 8 forever after.
        self._backfill_embed_state()
    self.db.commit()
```

`MIGRATIONS[8]` is a comment-only marker that bumps `schema_version`
to 8. The actual backfill is a Python step gated on the captured
`old_current`, because it has to run *after* `_ensure_vec_table()`
materializes the vec0 virtual table.

`_backfill_embed_state` keeps a defensive fast-path at the top — if
every memory already has an `embed_state` row, return immediately,
without touching vec0. This makes the function safe to call from
tests or future code paths without re-introducing the slow scan.

After this change, opening a fully-migrated index runs zero vec0
scans and completes in milliseconds even on a 1+ GB index.

### Decision 2 — CLI delegates read paths to the daemon when reachable

A new module `memstem.client` exposes a sync `DaemonClient` that
talks to the existing HTTP server (`POST /search`, `GET /memory/{id}`,
`GET /health`). The CLI uses it on read paths:

```text
memstem search "Kinsta"
  → probe GET /health (250ms timeout)
  → if up and serving the same vault path:
      POST /search           → render results, exit
  → if down, mismatched, or fails:
      open Index directly    → Search(...)        → render, exit
```

A `--no-daemon` flag forces the direct-DB path for debugging.

The HTTP server already exists and already mirrors the MCP tool list;
this PR just wires the CLI to it. The fallback path is identical to
the pre-PR behavior, so users without a daemon are unaffected.

`memstem reindex`, `memstem migrate`, `memstem daemon`, `memstem
embed`, and other commands that mutate or own the database continue
to open the index directly — those paths are inherently single-writer
and don't benefit from delegation.

### Decision 3 — CLI exposes phase progress on long-running ops

`memstem search`, `doctor`, `reindex`, `embed`, `migrate`, and
`daemon` accept `-v`/`--verbose`, which prints structured phase
markers with elapsed wall-clock to stderr:

```text
[memstem] connect:start
[memstem] connect:done elapsed=0.02s
[memstem] search:start
[memstem] search:done elapsed=0.18s results=5
```

In non-verbose mode, the CLI stays quiet for fast operations but
prints a single warning to stderr if `connect()` exceeds 2 s — so
future regressions of the same shape become visible without
requiring py-spy.

## Consequences

### Positive

- **CLI search returns in <500 ms on a 1+ GB index** with daemon
  running, and in <1.5 s on direct-DB fallback (a fresh embedder
  + sqlite_vec load + one OpenAI roundtrip).
- **Future migration backfills are bounded.** Any future migration
  that needs to scan the vault appends to `MIGRATIONS` and is gated
  on the captured `old_current`. The "runs on every connect" footgun
  is closed at the structural level.
- **One source of truth for read traffic.** When the daemon is up,
  CLI/MCP/HTTP all share the same hot connection, the same WAL
  cursor, the same embedder. No "did I just embed this query in two
  processes?" surprise.
- **Diagnostics now ship in-band.** A 30-s `connect()` produces a
  warning instead of a silent hang.

### Negative

- **CLI now has a soft dependency on a daemon being reachable** for
  best performance. The fallback path keeps it correct, but users
  running CLI-only deployments won't see the perf improvement until
  they start a daemon.
- **Two read code paths** (HTTP client + direct `Search`) need to be
  kept in sync. The HTTP server already serializes through the same
  `Search` class, so the divergence is limited to transport, but
  it's a real surface to maintain.
- **One-time slow upgrade for legacy v7 installs.** The first
  `Index.connect()` after upgrading runs the v8 backfill marker
  migration. With the fast-path pre-check, this is milliseconds for
  any vault whose `embed_state` is already populated. Vaults that
  genuinely need backfilling (v3-era data that never had it run)
  still pay the legacy cost once, but only once.

### Neutral

- **`SCHEMA_VERSION` advances from 7 → 8.** The v8 migration is
  comment-only at the SQL level; the meaningful work is the
  `_backfill_embed_state()` call gated on `old_current < 8`.
- **MCP server is unchanged.** It already runs inside the daemon
  process and shares its connection.

## Implementation

Sequenced as three PRs:

1. **`fix(index): run embed_state backfill once during migration`** —
   Decision 1. Smallest surface, ships first to fix the immediate
   pain on existing deployments.
2. **`feat(cli): delegate search to daemon when reachable`** —
   Decision 2. New module + CLI wiring + tests for daemon up / down /
   vault mismatch / `--no-daemon` paths.
3. **`feat(cli): structured phase progress on long-running ops`** —
   Decision 3. Polish, but ships under this ADR because it directly
   prevents the "silent hang" diagnosis problem that produced this
   ADR in the first place.

Each PR carries its own tests and CHANGELOG entry. PR 2 and PR 3
build on PR 1's branch and are intended to be merged in order.

## Alternatives considered

**Add a vec0 auxiliary index on `memory_id`.** vec0's auxiliary-
column syntax (`+memory_id TEXT`) would in principle make the EXISTS
subquery fast, but it requires a `memories_vec` rebuild and changes
the storage layout for every existing user. The migration-discipline
fix achieves the same end result (no slow vec0 scan) without
touching the storage layout.

**CLI shells out to `memstem mcp` over stdio.** Reuses the MCP code
path but pays full process-spawn cost on every CLI call (Python
import, MCP handshake). Worse on cold-cache than direct-DB.

**Replace the CLI's `Search` import with the HTTP client and require
the daemon.** Forces every CLI user to run a daemon. Breaks the
"works on a developer laptop without setup" story from ADR 0001.

**Rebuild the index from markdown on every connect.** Solves the
backfill problem by removing the index. Makes search latency
proportional to vault size on every query. Hard no.
