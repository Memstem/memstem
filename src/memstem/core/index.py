"""SQLite index over the canonical vault: FTS5 + sqlite-vec hybrid search.

The markdown vault is canonical. This index is derived and rebuildable, so
schema changes can ship as a wipe-and-rebuild operation without data loss.

Schema is versioned via a single-row `schema_version` table. New migrations
append to the `MIGRATIONS` mapping; `connect()` advances the version on open.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import sqlite_vec

from memstem.core.frontmatter import Frontmatter
from memstem.core.storage import Memory

SCHEMA_VERSION = 1
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")


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
}


def extract_wikilinks(body: str) -> list[str]:
    """Return the list of `[[wikilink]]` targets in body order, preserving duplicates."""
    return [match.strip() for match in WIKILINK_RE.findall(body)]


def _serialize_vector(embedding: Iterable[float]) -> bytes:
    floats = list(embedding)
    return struct.pack(f"{len(floats)}f", *floats)


class Index:
    """SQLite index with FTS5 + sqlite-vec virtual tables."""

    def __init__(self, db_path: Path | str, dimensions: int = 768) -> None:
        self.db_path = Path(db_path)
        self.dimensions = dimensions
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("index is not connected; call connect() first")
        return self._db

    def connect(self) -> None:
        if self._db is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA foreign_keys = ON")
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
            row = self.db.execute("SELECT version FROM schema_version").fetchone()
            current = int(row["version"]) if row else 0
        except sqlite3.OperationalError:
            current = 0

        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            self.db.executescript(MIGRATIONS[version])
            self.db.execute(
                "INSERT OR REPLACE INTO schema_version(rowid, version) VALUES (1, ?)",
                (version,),
            )

        self._ensure_vec_table()
        self.db.commit()

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
        """
        fm = memory.frontmatter
        memory_id = str(fm.id)
        path_str = str(memory.path)
        params = self._memory_params(fm, memory.body, path_str)

        with self.db:
            self.db.execute("DELETE FROM tags WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM links WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self.db.execute(
                """
                INSERT OR REPLACE INTO memories (
                    id, type, source, title, body, path,
                    created, updated, importance, confidence,
                    valid_from, valid_to, embedding_version, deprecated_by
                ) VALUES (
                    :id, :type, :source, :title, :body, :path,
                    :created, :updated, :importance, :confidence,
                    :valid_from, :valid_to, :embedding_version, :deprecated_by
                )
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

        with self.db:
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
        with self.db:
            self.db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

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
    "extract_wikilinks",
]
