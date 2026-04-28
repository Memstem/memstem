"""SQLite index over the canonical vault: FTS5 + sqlite-vec hybrid search.

The markdown vault is canonical. This index is derived and rebuildable, so
schema changes can ship as a wipe-and-rebuild operation without data loss.

Schema is versioned via a single-row `schema_version` table. New migrations
append to the `MIGRATIONS` mapping; `connect()` advances the version on open.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import sqlite_vec

from memstem.core.frontmatter import Frontmatter
from memstem.core.storage import Memory

SCHEMA_VERSION = 5
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")


def body_hash(body: str) -> str:
    """Stable hash of a body string used to detect content changes.

    SHA-256 of the UTF-8-encoded body, hex-encoded. Used as the cheap
    side of the "did the body change since we last embedded?" check —
    if the hash matches what's recorded in `embed_state`, the existing
    vectors are still valid and we can skip re-embedding.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FtsHit:
    memory_id: str
    score: float
    """BM25 score (lower = better; SQLite returns negative ranks)."""


@dataclass(frozen=True, slots=True)
class VecHit:
    memory_id: str
    chunk_id: str
    chunk_index: int
    distance: float
    """L2 distance (lower = closer)."""


MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY);

        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT,
            body TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            importance REAL,
            confidence TEXT,
            valid_from TEXT,
            valid_to TEXT,
            embedding_version INTEGER,
            deprecated_by TEXT
        );
        CREATE INDEX idx_memories_type ON memories(type);
        CREATE INDEX idx_memories_source ON memories(source);
        CREATE INDEX idx_memories_created ON memories(created);
        CREATE INDEX idx_memories_updated ON memories(updated);
        CREATE INDEX idx_memories_importance ON memories(importance);

        CREATE TABLE tags (
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (memory_id, tag)
        );
        CREATE INDEX idx_tags_tag ON tags(tag);

        CREATE TABLE links (
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            PRIMARY KEY (memory_id, target)
        );
        CREATE INDEX idx_links_target ON links(target);

        CREATE VIRTUAL TABLE memories_fts USING fts5(
            memory_id UNINDEXED,
            title,
            body,
            tags,
            tokenize='porter unicode61'
        );
    """,
    2: """
        -- Records that need vector embedding. The pipeline writes here
        -- synchronously after vault + FTS5 are persisted; the embedder
        -- worker drains the queue at its own pace.
        CREATE TABLE IF NOT EXISTS embed_queue (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            enqueued_at TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            failed INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_embed_queue_failed ON embed_queue(failed);
    """,
    3: """
        -- Records what each memory was last successfully embedded with.
        -- The pipeline reads this to decide whether to re-enqueue a
        -- record on re-emit; if the body hasn't changed and the embedder
        -- signature still matches, the existing vectors are valid and
        -- enqueueing would just burn rate-limit quota.
        --
        -- The worker writes here on every successful embed. On schema
        -- v3 first-open we backfill rows for memories that already have
        -- vectors, with `embed_signature = NULL` (legacy/grandfathered
        -- — treated as compatible with any signature until the body
        -- changes or the user runs `memstem reindex`).
        CREATE TABLE IF NOT EXISTS embed_state (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            body_hash TEXT NOT NULL,
            embed_signature TEXT,
            embedded_at TEXT NOT NULL
        );
    """,
    4: """
        -- Layer 1 of the dedup pipeline (ADR 0012). Maps the SHA-256 hash
        -- of a normalized body (whitespace-collapsed, lowercased) to the
        -- canonical memory_id that stores that body.
        --
        -- A second record arriving with the same hash under a different
        -- (source, ref) is treated as a duplicate: the pipeline skips
        -- writing it and bumps `seen_count` on the existing row instead.
        -- That gives us an audit trail for "how many times did the same
        -- content try to enter the index?" — the kind of signal that
        -- catches recall feedback loops (mem0's 808-copy failure mode).
        --
        -- ON DELETE CASCADE keeps the index clean: when the canonical
        -- memory is deleted, its hash row goes too. Single-row-per-hash
        -- (PRIMARY KEY on body_hash) is intentional — there is at most
        -- one canonical owner per body.
        CREATE TABLE IF NOT EXISTS body_hash_index (
            body_hash TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            seen_count INTEGER NOT NULL DEFAULT 1,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_body_hash_index_memory_id
            ON body_hash_index(memory_id);
    """,
    5: """
        -- ADR 0008 Tier 1 query log: the hygiene worker reads this to
        -- bump importance on memories the user actually retrieved. Each
        -- row records ONE memory's exposure inside ONE query result list
        -- (or one `memstem_get` open). A 10-result search produces 10
        -- rows; a single get produces 1.
        --
        -- This table is intentionally non-canonical — losing it during
        -- a crash drifts importance back toward heuristic-only, which
        -- the rest of the system tolerates. Storing it inside the
        -- existing `_meta/index.db` keeps backups simple (one file).
        --
        -- Bounded by a row cap (`hygiene.query_log_max_rows`, default
        -- 100k) enforced at write time. The auto-increment id lets the
        -- hygiene worker scan only "rows since last sweep" without
        -- needing a side cursor.
        --
        -- ON DELETE CASCADE keeps the log honest: if a memory is removed
        -- from the vault, its retrieval rows go with it (the hygiene
        -- worker shouldn't credit deleted records).
        CREATE TABLE IF NOT EXISTS query_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            query TEXT,
            client TEXT,
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            rank INTEGER,
            score REAL
        );
        CREATE INDEX IF NOT EXISTS idx_query_log_ts ON query_log(ts);
        CREATE INDEX IF NOT EXISTS idx_query_log_memory_id ON query_log(memory_id);
        CREATE INDEX IF NOT EXISTS idx_query_log_kind ON query_log(kind);
    """,
}


def extract_wikilinks(body: str) -> list[str]:
    """Return the list of `[[wikilink]]` targets in body order, preserving duplicates."""
    return [match.strip() for match in WIKILINK_RE.findall(body)]


def _serialize_vector(embedding: Iterable[float]) -> bytes:
    floats = list(embedding)
    return struct.pack(f"{len(floats)}f", *floats)


class Index:
    """SQLite index with FTS5 + sqlite-vec virtual tables.

    Thread-safety: the connection is opened with `check_same_thread=False`
    so the embed worker can hand SQLite calls off via
    :func:`asyncio.to_thread`, but Python's `sqlite3` module isn't
    actually thread-safe on a single connection — concurrent commits
    race, and the sqlite-vec extension keeps thread-local state that
    breaks under cross-thread use. We resolve both by serializing every
    write/read path through ``self._lock``. Acquisition is cheap (no
    contention in single-worker setups; mild contention with multiple
    workers) and far less complex than a per-thread connection pool.
    The lock does NOT cover embed HTTP calls — those happen above this
    class — so worker concurrency on the network side is preserved.
    """

    def __init__(self, db_path: Path | str, dimensions: int = 768) -> None:
        self.db_path = Path(db_path)
        self.dimensions = dimensions
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("index is not connected; call connect() first")
        return self._db

    def connect(self) -> None:
        if self._db is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` lets the embed worker run sync SQLite
        # calls inside `asyncio.to_thread`. sqlite3 is thread-safe at the
        # library level (Python builds default to SQLITE_THREADSAFE=1);
        # our queue serializes writes per memory_id so no two workers
        # ever target the same row in the same instant.
        db = sqlite3.connect(self.db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA foreign_keys = ON")
        # Concurrency hardening: WAL mode lets readers run alongside a
        # writer (instead of blocking), and busy_timeout makes both
        # readers and writers wait up to 5s for a lock instead of
        # failing immediately with `database is locked`. Without these,
        # a CLI invocation (e.g. `memstem reindex`) running while the
        # daemon is writing can crash either side intermittently.
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA busy_timeout = 5000")
        self._db = db
        self._migrate()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _migrate(self) -> None:
        try:
            row = self.db.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            current = int(row["version"]) if row else 0
        except sqlite3.OperationalError:
            current = 0

        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            self.db.executescript(MIGRATIONS[version])
            # `version` is the PRIMARY KEY of schema_version, so we keep
            # exactly one row by deleting first. Older code stored rowid=1
            # which fought with the auto-aliasing of INTEGER PRIMARY KEY.
            self.db.execute("DELETE FROM schema_version")
            self.db.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))

        self._ensure_vec_table()
        self._backfill_embed_state()
        self.db.commit()

    def _backfill_embed_state(self) -> None:
        """Populate `embed_state` for memories that already have vectors.

        Runs every time the index opens; idempotent. The expected
        workload is the v3 upgrade, where it stamps every previously-
        embedded memory in one pass so the daemon's next reconcile
        doesn't re-enqueue them. Subsequent boots are a no-op.

        Uses ``INSERT OR IGNORE`` so the helper survives the race
        where two connections (e.g. an MCP child and a CLI invocation)
        open the same vault simultaneously: both SELECTs return the
        same un-stamped rows, both try to INSERT, and the loser used
        to crash with ``UNIQUE constraint failed: embed_state.memory_id``.
        The ``NOT EXISTS`` guard in the SELECT narrows the window but
        cannot close it; OR IGNORE closes it.

        The signature is left NULL — we don't know what embedder
        produced the existing vectors, so we shouldn't claim to. NULL
        is treated as "compatible with any current signature" by
        :meth:`needs_reembed`, so legacy records keep their vectors
        until the body changes (which re-embeds and stamps the real
        signature) or until the user runs ``memstem reindex``.
        """
        rows = self.db.execute(
            """
            SELECT m.id AS id, m.body AS body
            FROM memories m
            WHERE EXISTS (SELECT 1 FROM memories_vec v WHERE v.memory_id = m.id)
              AND NOT EXISTS (SELECT 1 FROM embed_state s WHERE s.memory_id = m.id)
            """
        ).fetchall()
        if not rows:
            return
        now = datetime.now(tz=UTC).isoformat()
        payload = [(r["id"], body_hash(r["body"]), None, now) for r in rows]
        self.db.executemany(
            """
            INSERT OR IGNORE INTO embed_state(memory_id, body_hash, embed_signature, embedded_at)
            VALUES (?, ?, ?, ?)
            """,
            payload,
        )

    def _ensure_vec_table(self) -> None:
        # vec0 virtual tables can't be created in executescript() reliably across
        # versions, and the column dimensions depend on the embedder, so we
        # create it explicitly after the main schema is in place.
        self.db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                chunk_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                embedding FLOAT[{self.dimensions}]
            )
            """
        )

    def upsert(self, memory: Memory) -> None:
        """Insert or replace a memory's metadata, tags, links, and FTS row.

        Vector rows are managed separately via `upsert_vectors` so callers can
        chunk + embed at their own cadence.

        If another row already occupies this `path` under a different id
        (e.g. an MCP-driven upsert with a custom path that shadows an
        existing record), its tags/links/FTS/vec rows are cleaned up so
        nothing orphans in the index.
        """
        fm = memory.frontmatter
        memory_id = str(fm.id)
        path_str = str(memory.path)
        params = self._memory_params(fm, memory.body, path_str)

        with self._lock, self.db:
            displaced = self.db.execute(
                "SELECT id FROM memories WHERE path = ? AND id != ?",
                (path_str, memory_id),
            ).fetchone()
            if displaced is not None:
                old_id = displaced["id"]
                self.db.execute("DELETE FROM tags WHERE memory_id = ?", (old_id,))
                self.db.execute("DELETE FROM links WHERE memory_id = ?", (old_id,))
                self.db.execute("DELETE FROM memories_fts WHERE memory_id = ?", (old_id,))
                self.db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (old_id,))
                self.db.execute("DELETE FROM memories WHERE id = ?", (old_id,))

            self.db.execute("DELETE FROM tags WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM links WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            # `INSERT ... ON CONFLICT DO UPDATE` (not `INSERT OR REPLACE`)
            # so that re-upserting a row doesn't trigger ON DELETE CASCADE
            # on child tables. With REPLACE, SQLite implements the
            # "replace" as DELETE-then-INSERT, which cascade-wipes
            # `embed_state` and `embed_queue` rows referencing the same
            # id — that would erase the worker's hard-won "this content
            # is already embedded with signature X" record on every
            # reconcile pass. ON CONFLICT DO UPDATE leaves the row in
            # place and just rewrites its columns, so child references
            # survive.
            self.db.execute(
                """
                INSERT INTO memories (
                    id, type, source, title, body, path,
                    created, updated, importance, confidence,
                    valid_from, valid_to, embedding_version, deprecated_by
                ) VALUES (
                    :id, :type, :source, :title, :body, :path,
                    :created, :updated, :importance, :confidence,
                    :valid_from, :valid_to, :embedding_version, :deprecated_by
                )
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    source = excluded.source,
                    title = excluded.title,
                    body = excluded.body,
                    path = excluded.path,
                    created = excluded.created,
                    updated = excluded.updated,
                    importance = excluded.importance,
                    confidence = excluded.confidence,
                    valid_from = excluded.valid_from,
                    valid_to = excluded.valid_to,
                    embedding_version = excluded.embedding_version,
                    deprecated_by = excluded.deprecated_by
                """,
                params,
            )

            if fm.tags:
                self.db.executemany(
                    "INSERT INTO tags(memory_id, tag) VALUES (?, ?)",
                    [(memory_id, t) for t in fm.tags],
                )

            link_targets = set(fm.links) | set(extract_wikilinks(memory.body))
            if link_targets:
                self.db.executemany(
                    "INSERT INTO links(memory_id, target) VALUES (?, ?)",
                    [(memory_id, t) for t in sorted(link_targets)],
                )

            self.db.execute(
                """
                INSERT INTO memories_fts(memory_id, title, body, tags)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, fm.title or "", memory.body, " ".join(fm.tags)),
            )

    def upsert_vectors(
        self,
        memory_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        """Replace the vector rows for `memory_id` with one row per chunk."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must be the same length")
        for vec in embeddings:
            if len(vec) != self.dimensions:
                raise ValueError(f"embedding dim {len(vec)} != index dim {self.dimensions}")

        with self._lock, self.db:
            self.db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
            for i, (chunk, vec) in enumerate(zip(chunks, embeddings, strict=True)):
                chunk_id = f"{memory_id}:{i}"
                self.db.execute(
                    """
                    INSERT INTO memories_vec(chunk_id, memory_id, chunk_index, embedding)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chunk_id, memory_id, i, _serialize_vector(vec)),
                )
                # silence unused-var warning while keeping `chunk` available for
                # future hygiene-worker hooks (skill extraction reads chunks)
                _ = chunk

    def delete(self, memory_id: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM embed_queue WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM embed_state WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    # ---- Embed state ------------------------------------------------------

    def needs_reembed(self, memory_id: str, content_hash: str, embed_signature: str) -> bool:
        """Return True if this memory needs (re-)embedding.

        Checks three conditions in order:
        - No rows in `memories_vec` for this id → never embedded.
        - No `embed_state` row → first time we're being asked, embed.
        - `body_hash` differs → content changed, vectors are stale.
        - `embed_signature` differs and stored value is non-NULL →
          embedder configuration changed.

        A NULL stored signature is the legacy/grandfathered marker
        (see :meth:`_backfill_embed_state`): treat it as compatible
        with whatever the caller is asking about, so we don't pointlessly
        re-embed every record on the first daemon start after upgrading
        to schema v3.
        """
        with self._lock:
            vec = self.db.execute(
                "SELECT 1 FROM memories_vec WHERE memory_id = ? LIMIT 1",
                (memory_id,),
            ).fetchone()
            if vec is None:
                return True
            state = self.db.execute(
                "SELECT body_hash, embed_signature FROM embed_state WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        if state is None:
            return True
        if state["body_hash"] != content_hash:
            return True
        stored_sig = state["embed_signature"]
        if stored_sig is not None and stored_sig != embed_signature:
            return True
        return False

    def record_embed_state(self, memory_id: str, content_hash: str, embed_signature: str) -> None:
        """Mark `memory_id` as freshly embedded with the given hash + signature.

        Called by the embed worker after a successful vector upsert.
        Idempotent: re-recording the same state is fine (the
        ``embedded_at`` timestamp gets bumped).
        """
        now = datetime.now(tz=UTC).isoformat()
        with self._lock, self.db:
            self.db.execute(
                """
                INSERT INTO embed_state(memory_id, body_hash, embed_signature, embedded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    body_hash = excluded.body_hash,
                    embed_signature = excluded.embed_signature,
                    embedded_at = excluded.embedded_at
                """,
                (memory_id, content_hash, embed_signature, now),
            )

    # ---- Embed queue ------------------------------------------------------

    def enqueue_embed(self, memory_id: str) -> None:
        """Mark `memory_id` as needing vector embedding.

        Idempotent: re-enqueueing an existing entry resets `retry_count`
        and `failed=0` so a record that previously gave up gets another
        try when its content changes.
        """
        now = datetime.now(tz=UTC).isoformat()
        with self._lock, self.db:
            self.db.execute(
                """
                INSERT INTO embed_queue(memory_id, enqueued_at, retry_count, last_error, failed)
                VALUES (?, ?, 0, NULL, 0)
                ON CONFLICT(memory_id) DO UPDATE SET
                    enqueued_at = excluded.enqueued_at,
                    retry_count = 0,
                    last_error = NULL,
                    failed = 0
                """,
                (memory_id, now),
            )

    def dequeue_embed(self, memory_id: str) -> None:
        """Remove `memory_id` from the queue (called after a successful embed)."""
        with self._lock, self.db:
            self.db.execute("DELETE FROM embed_queue WHERE memory_id = ?", (memory_id,))

    def mark_embed_error(
        self,
        memory_id: str,
        error: str,
        max_retries: int = 5,
    ) -> None:
        """Record a transient embed failure.

        Increments `retry_count`; once it exceeds `max_retries` the row
        is flipped to `failed=1` and skipped by future drains until the
        record changes (which re-enqueues with reset state) or the user
        runs `memstem embed --retry-failed`.
        """
        with self._lock, self.db:
            self.db.execute(
                """
                UPDATE embed_queue
                SET retry_count = retry_count + 1,
                    last_error = ?,
                    failed = CASE WHEN retry_count + 1 >= ? THEN 1 ELSE 0 END
                WHERE memory_id = ?
                """,
                (error[:500], max_retries, memory_id),
            )

    def queue_pending(self, limit: int) -> list[str]:
        """Return up to `limit` memory_ids that are pending embedding (not failed)."""
        with self._lock:
            rows = self.db.execute(
                """
                SELECT memory_id FROM embed_queue
                WHERE failed = 0
                ORDER BY enqueued_at ASC, retry_count ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [r["memory_id"] for r in rows]

    def get_path(self, memory_id: str) -> str | None:
        """Return the vault-relative path for a memory id, or None if missing.

        Properly serialized through the Index lock — the embed worker
        used to call `index.db.execute` directly which races other
        threads and intermittently triggers `sqlite3.InterfaceError:
        bad parameter or other API misuse` when a concurrent operation
        leaves the connection state inconsistent.
        """
        with self._lock:
            row = self.db.execute("SELECT path FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return row["path"] if row else None

    def queue_stats(self) -> dict[str, int]:
        """Return `{pending, failed, total}` for `memstem doctor`."""
        with self._lock:
            row = self.db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN failed = 0 THEN 1 ELSE 0 END), 0) AS pending,
                    COALESCE(SUM(CASE WHEN failed = 1 THEN 1 ELSE 0 END), 0) AS failed,
                    COUNT(*) AS total
                FROM embed_queue
                """
            ).fetchone()
        return {
            "pending": int(row["pending"]),
            "failed": int(row["failed"]),
            "total": int(row["total"]),
        }

    def reset_failed_queue(self) -> int:
        """Mark every `failed=1` row pending again. Returns rows reset."""
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE embed_queue SET failed = 0, retry_count = 0, last_error = NULL "
                "WHERE failed = 1"
            )
        return int(cur.rowcount)

    def query_fts(
        self,
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[FtsHit]:
        sql = """
            SELECT f.memory_id AS memory_id, bm25(memories_fts) AS score
            FROM memories_fts f
            JOIN memories m ON m.id = f.memory_id
            WHERE memories_fts MATCH ?
        """
        params: list[Any] = [query]
        if types:
            placeholders = ",".join("?" for _ in types)
            sql += f" AND m.type IN ({placeholders})"
            params.extend(types)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [FtsHit(memory_id=r["memory_id"], score=float(r["score"])) for r in rows]

    def query_vec(
        self,
        embedding: list[float],
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[VecHit]:
        if len(embedding) != self.dimensions:
            raise ValueError(f"query embedding dim {len(embedding)} != index dim {self.dimensions}")
        # Over-fetch from vec then filter by type, since vec0 doesn't support
        # arbitrary predicates inside the MATCH clause.
        fetch_k = limit * 5 if types else limit
        with self._lock:
            rows = self.db.execute(
                """
                SELECT v.chunk_id, v.memory_id, v.chunk_index, v.distance
                FROM memories_vec v
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (_serialize_vector(embedding), fetch_k),
            ).fetchall()

            if types:
                type_set = set(types)
                id_rows = (
                    self.db.execute(
                        f"""
                    SELECT id, type FROM memories
                    WHERE id IN ({",".join("?" for _ in {r["memory_id"] for r in rows})})
                    """,
                        [r["memory_id"] for r in rows],
                    ).fetchall()
                    if rows
                    else []
                )
                allowed = {r["id"] for r in id_rows if r["type"] in type_set}
                rows = [r for r in rows if r["memory_id"] in allowed][:limit]

        return [
            VecHit(
                memory_id=r["memory_id"],
                chunk_id=r["chunk_id"],
                chunk_index=int(r["chunk_index"]),
                distance=float(r["distance"]),
            )
            for r in rows
        ]

    @staticmethod
    def _memory_params(fm: Frontmatter, body: str, path: str) -> dict[str, Any]:
        return {
            "id": str(fm.id),
            "type": fm.type.value,
            "source": fm.source,
            "title": fm.title,
            "body": body,
            "path": path,
            "created": fm.created.isoformat(),
            "updated": fm.updated.isoformat(),
            "importance": fm.importance,
            "confidence": fm.confidence.value if fm.confidence else None,
            "valid_from": fm.valid_from.isoformat() if fm.valid_from else None,
            "valid_to": fm.valid_to.isoformat() if fm.valid_to else None,
            "embedding_version": fm.embedding_version,
            "deprecated_by": str(fm.deprecated_by) if fm.deprecated_by else None,
        }


__all__ = [
    "FtsHit",
    "Index",
    "VecHit",
    "body_hash",
    "extract_wikilinks",
]
