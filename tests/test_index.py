"""Tests for the SQLite + FTS5 + sqlite-vec index."""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.core.frontmatter import Frontmatter, validate
from memstem.core.index import Index, body_hash, extract_wikilinks
from memstem.core.storage import Memory


def _make_memory(
    *,
    type_: str = "memory",
    title: str | None = "test",
    body: str = "hello world",
    tags: list[str] | None = None,
    links: list[str] | None = None,
    importance: float | None = None,
    path: str | None = None,
    scope: str | None = None,
    verification: str | None = None,
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title,
        "tags": tags or [],
        "links": links or [],
    }
    if importance is not None:
        metadata["importance"] = importance
    if scope is not None:
        metadata["scope"] = scope
    if verification is not None:
        metadata["verification"] = verification
    fm: Frontmatter = validate(metadata)
    return Memory(
        frontmatter=fm,
        body=body,
        path=Path(path or f"memories/{fm.id}.md"),
    )


def _fake_embedding(seed: int, dims: int = 768) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 1.0) for _ in range(dims)]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


class TestExtractWikilinks:
    def test_finds_simple_links(self) -> None:
        body = "See [[Brad Besner]] and [[Cloudflare]]."
        assert extract_wikilinks(body) == ["Brad Besner", "Cloudflare"]

    def test_returns_empty_for_plain_text(self) -> None:
        assert extract_wikilinks("no links here") == []

    def test_preserves_order_with_duplicates(self) -> None:
        assert extract_wikilinks("[[A]] [[B]] [[A]]") == ["A", "B", "A"]


class TestSchema:
    def test_connect_creates_tables(self, index: Index) -> None:
        rows = index.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual') ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        for required in {
            "memories",
            "memories_fts",
            "memories_vec",
            "tags",
            "links",
            "schema_version",
        }:
            assert required in names

    def test_schema_version_recorded(self, index: Index) -> None:
        version = index.db.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"]
        # Bumped to 11 in ADRs 0020/0021 (summarizer_cache table).
        assert version == 11

    def test_connect_is_idempotent(self, index: Index) -> None:
        # Second connect on the same instance should be a no-op.
        index.connect()
        rows = index.db.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1

    def test_reopen_existing_db_does_not_re_migrate(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        Index(db_path).connect()
        # Reopen and confirm schema_version still has exactly one row at
        # the latest version (no duplicate inserts on every connect).
        idx = Index(db_path)
        idx.connect()
        try:
            rows = idx.db.execute("SELECT version FROM schema_version").fetchall()
            assert [r["version"] for r in rows] == [11]
        finally:
            idx.close()


class TestUpsert:
    def test_round_trip_basic_fields(self, index: Index) -> None:
        memory = _make_memory(title="brad", body="hello", importance=0.7)
        index.upsert(memory)
        row = index.db.execute(
            "SELECT id, title, body, importance FROM memories WHERE id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["id"] == str(memory.id)
        assert row["title"] == "brad"
        assert row["body"] == "hello"
        assert row["importance"] == 0.7

    def test_upsert_with_tags(self, index: Index) -> None:
        memory = _make_memory(tags=["alpha", "beta"])
        index.upsert(memory)
        rows = index.db.execute(
            "SELECT tag FROM tags WHERE memory_id = ? ORDER BY tag",
            (str(memory.id),),
        ).fetchall()
        assert [r["tag"] for r in rows] == ["alpha", "beta"]

    def test_upsert_extracts_wikilinks_from_body(self, index: Index) -> None:
        memory = _make_memory(body="ref to [[Cloudflare]] and [[Vault]]")
        index.upsert(memory)
        rows = index.db.execute(
            "SELECT target FROM links WHERE memory_id = ? ORDER BY target",
            (str(memory.id),),
        ).fetchall()
        assert [r["target"] for r in rows] == ["Cloudflare", "Vault"]

    def test_upsert_merges_frontmatter_and_body_links(self, index: Index) -> None:
        memory = _make_memory(
            body="see [[Body Target]]",
            links=["FM Target"],
        )
        index.upsert(memory)
        rows = index.db.execute(
            "SELECT target FROM links WHERE memory_id = ? ORDER BY target",
            (str(memory.id),),
        ).fetchall()
        assert {r["target"] for r in rows} == {"Body Target", "FM Target"}

    def test_upsert_replaces_existing(self, index: Index) -> None:
        memory = _make_memory(title="v1", tags=["original"])
        index.upsert(memory)

        # Construct a new Memory with the same id but updated content.
        updated_fm = validate(
            {
                **memory.frontmatter.model_dump(mode="json"),
                "title": "v2",
                "tags": ["replaced"],
            }
        )
        index.upsert(Memory(frontmatter=updated_fm, body="new body", path=memory.path))

        title = index.db.execute(
            "SELECT title FROM memories WHERE id = ?", (str(memory.id),)
        ).fetchone()["title"]
        assert title == "v2"

        tags = [
            r["tag"]
            for r in index.db.execute(
                "SELECT tag FROM tags WHERE memory_id = ?", (str(memory.id),)
            ).fetchall()
        ]
        assert tags == ["replaced"]

    def test_upsert_evicts_displaced_path_holder(self, index: Index) -> None:
        """When a new memory takes a path that was held by a different id,
        the old row's tags/links/FTS/vec rows are cleaned up so the index
        doesn't accumulate orphans (as it did pre-PR-25 during migrate)."""
        old = _make_memory(title="old", tags=["a"], path="daily/2026-04-26.md")
        index.upsert(old)
        index.upsert_vectors(str(old.id), ["chunk"], [_fake_embedding(1)])

        # New record claims the same path under a different id (e.g. an
        # MCP upsert that overwrites a daily file).
        new = _make_memory(title="new", tags=["b"], path="daily/2026-04-26.md")
        assert str(new.id) != str(old.id)
        index.upsert(new)

        # Old row gone.
        assert (
            index.db.execute("SELECT id FROM memories WHERE id = ?", (str(old.id),)).fetchone()
            is None
        )
        # New row present.
        row = index.db.execute("SELECT title FROM memories WHERE id = ?", (str(new.id),)).fetchone()
        assert row["title"] == "new"
        # No orphans in tags / FTS / vec for the displaced id.
        for table in ("tags", "links", "memories_fts", "memories_vec"):
            count = index.db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE memory_id = ?",
                (str(old.id),),
            ).fetchone()["c"]
            assert count == 0, f"{table} still has orphan rows for displaced id"

    def test_delete_cascades(self, index: Index) -> None:
        memory = _make_memory(tags=["a", "b"], body="see [[X]]", links=["Y"])
        index.upsert(memory)
        index.upsert_vectors(str(memory.id), ["chunk"], [_fake_embedding(1)])

        index.delete(str(memory.id))

        for table in ("memories", "tags", "links", "memories_vec"):
            count = index.db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            assert count == 0, f"{table} not cleared"


class TestVectorStorage:
    def test_round_trip(self, index: Index) -> None:
        memory = _make_memory()
        index.upsert(memory)
        vec = _fake_embedding(42)
        index.upsert_vectors(str(memory.id), ["only chunk"], [vec])
        row = index.db.execute(
            "SELECT chunk_index FROM memories_vec WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["chunk_index"] == 0

    def test_dimension_mismatch_rejected(self, index: Index) -> None:
        memory = _make_memory()
        index.upsert(memory)
        with pytest.raises(ValueError, match="embedding dim"):
            index.upsert_vectors(str(memory.id), ["chunk"], [[0.1, 0.2, 0.3]])

    def test_chunk_count_mismatch_rejected(self, index: Index) -> None:
        memory = _make_memory()
        index.upsert(memory)
        with pytest.raises(ValueError, match="same length"):
            index.upsert_vectors(str(memory.id), ["one", "two"], [_fake_embedding(1)])

    def test_replaces_old_chunks(self, index: Index) -> None:
        memory = _make_memory()
        index.upsert(memory)
        index.upsert_vectors(
            str(memory.id),
            ["a", "b", "c"],
            [_fake_embedding(i) for i in range(3)],
        )
        index.upsert_vectors(str(memory.id), ["only"], [_fake_embedding(99)])
        count = index.db.execute(
            "SELECT COUNT(*) AS c FROM memories_vec WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()["c"]
        assert count == 1


class TestBodyHash:
    """`body_hash` is the cheap "did the content change?" check."""

    def test_stable_for_same_input(self) -> None:
        assert body_hash("hello") == body_hash("hello")

    def test_different_for_different_input(self) -> None:
        assert body_hash("hello") != body_hash("world")

    def test_handles_unicode(self) -> None:
        # Should not raise; output is hex regardless of input charset.
        h = body_hash("héllo 🌍")
        assert isinstance(h, str)
        assert len(h) == 64  # sha256 hex


class TestEmbedState:
    """`embed_state` records the last successful embedding's body+signature.

    Together with `memories_vec`, it lets `needs_reembed` answer
    "should we re-enqueue this record?" without redoing the embed work.
    """

    SIG_GEMINI = "gemini:gemini-embedding-2-preview:768"
    SIG_OLLAMA = "ollama:nomic-embed-text:768"

    def _seed_with_vectors(self, index: Index, body: str = "hello") -> Memory:
        memory = _make_memory(body=body)
        index.upsert(memory)
        index.upsert_vectors(str(memory.id), [body], [_fake_embedding(1)])
        return memory

    def test_needs_reembed_when_no_vectors(self, index: Index) -> None:
        memory = _make_memory(body="hello")
        index.upsert(memory)
        # No vec rows yet → must embed.
        assert index.needs_reembed(str(memory.id), body_hash("hello"), self.SIG_GEMINI)

    def test_needs_reembed_when_no_state_row(self, index: Index) -> None:
        # Vectors exist but no embed_state → must embed (shouldn't happen
        # in practice, but the helper has to handle it).
        memory = self._seed_with_vectors(index)
        index.db.execute("DELETE FROM embed_state WHERE memory_id = ?", (str(memory.id),))
        index.db.commit()
        assert index.needs_reembed(str(memory.id), body_hash("hello"), self.SIG_GEMINI)

    def test_needs_reembed_when_body_hash_differs(self, index: Index) -> None:
        memory = self._seed_with_vectors(index, body="v1")
        index.record_embed_state(str(memory.id), body_hash("v1"), self.SIG_GEMINI)
        # Body changed: hash no longer matches stored.
        assert index.needs_reembed(str(memory.id), body_hash("v2"), self.SIG_GEMINI)

    def test_needs_reembed_when_signature_differs(self, index: Index) -> None:
        memory = self._seed_with_vectors(index)
        index.record_embed_state(str(memory.id), body_hash("hello"), self.SIG_GEMINI)
        # Provider switched: signature mismatch forces re-embed.
        assert index.needs_reembed(str(memory.id), body_hash("hello"), self.SIG_OLLAMA)

    def test_no_reembed_when_unchanged(self, index: Index) -> None:
        memory = self._seed_with_vectors(index)
        index.record_embed_state(str(memory.id), body_hash("hello"), self.SIG_GEMINI)
        assert not index.needs_reembed(str(memory.id), body_hash("hello"), self.SIG_GEMINI)

    def test_null_signature_treated_as_compatible(self, index: Index) -> None:
        """Legacy backfilled rows have NULL signature; don't re-embed them
        for signature mismatch alone."""
        memory = self._seed_with_vectors(index)
        # Manual NULL signature, simulating a backfilled row.
        index.db.execute("DELETE FROM embed_state WHERE memory_id = ?", (str(memory.id),))
        index.db.execute(
            """
            INSERT INTO embed_state(memory_id, body_hash, embed_signature, embedded_at)
            VALUES (?, ?, NULL, '2026-04-26T00:00:00+00:00')
            """,
            (str(memory.id), body_hash("hello")),
        )
        index.db.commit()
        # Body matches; signature mismatch is shrugged off (legacy).
        assert not index.needs_reembed(str(memory.id), body_hash("hello"), self.SIG_GEMINI)
        # But body change still forces re-embed.
        assert index.needs_reembed(str(memory.id), body_hash("different"), self.SIG_GEMINI)

    def test_record_embed_state_upserts(self, index: Index) -> None:
        memory = self._seed_with_vectors(index)
        index.record_embed_state(str(memory.id), body_hash("a"), self.SIG_GEMINI)
        index.record_embed_state(str(memory.id), body_hash("b"), self.SIG_OLLAMA)
        rows = index.db.execute(
            "SELECT body_hash, embed_signature FROM embed_state WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["body_hash"] == body_hash("b")
        assert rows[0]["embed_signature"] == self.SIG_OLLAMA

    def test_delete_cascades_embed_state(self, index: Index) -> None:
        memory = self._seed_with_vectors(index)
        index.record_embed_state(str(memory.id), body_hash("hello"), self.SIG_GEMINI)
        index.delete(str(memory.id))
        row = index.db.execute(
            "SELECT 1 FROM embed_state WHERE memory_id = ?", (str(memory.id),)
        ).fetchone()
        assert row is None

    def test_record_embed_state_survives_parent_deletion_race(self, tmp_path: Path) -> None:
        """Regression: when the parent ``memories`` row is deleted
        between ``upsert_vectors`` and ``record_embed_state`` (the
        race that triggered ``sqlite3.IntegrityError: FOREIGN KEY
        constraint failed`` on Ari's box during the v0.7 → v0.8
        embedder migration), the worker must not crash. The cascade
        has already cleaned ``embed_queue`` / ``embed_state``;
        ``record_embed_state`` should silently no-op rather than
        raise. Vec rows the worker wrote pre-race are orphans and
        get cleaned in the same call to keep vec0 hygienic.
        """
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="will-be-deleted")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["chunk"], [[0.1] * 8])

            # Simulate the race: parent gets deleted while the embed
            # worker is still in flight (e.g., via path displacement
            # in a concurrent ``upsert``).
            idx.delete(str(memory.id))

            # Worker comes back from the embedder and tries to record
            # state. Pre-fix this raised IntegrityError; post-fix it
            # silently no-ops.
            idx.record_embed_state(str(memory.id), body_hash("will-be-deleted"), "sig:8")

            # No state row created (parent doesn't exist).
            state_row = idx.db.execute(
                "SELECT 1 FROM embed_state WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            assert state_row is None

            # Orphan vec rows cleaned — vec0 doesn't enforce FK so
            # without explicit cleanup these would leak.
            vec_row = idx.db.execute(
                "SELECT 1 FROM memories_vec WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            assert vec_row is None, (
                "orphan vec rows were left behind after FK race — "
                "next search would surface a memory that doesn't exist"
            )
        finally:
            idx.close()

    def test_record_embed_state_after_late_upsert_vectors(self, tmp_path: Path) -> None:
        """Tighter race shape: parent deleted, then ``upsert_vectors``
        runs (vec0 doesn't enforce FK so it succeeds), then
        ``record_embed_state`` fires the FK. We must still recover
        cleanly and not leave the orphan vec rows behind."""
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="late-vectors")
            idx.upsert(memory)
            idx.delete(str(memory.id))  # parent gone before any vectors written

            # Pretend the embed worker raced past the delete and wrote
            # vectors anyway (vec0 doesn't check; this is the orphan
            # producer). Then record_embed_state must clean them up.
            idx.upsert_vectors(str(memory.id), ["chunk"], [[0.2] * 8])
            assert (
                idx.db.execute(
                    "SELECT 1 FROM memories_vec WHERE memory_id = ?",
                    (str(memory.id),),
                ).fetchone()
                is not None
            )  # confirm the orphan exists

            idx.record_embed_state(str(memory.id), body_hash("late-vectors"), "sig:8")

            # Both state row absent AND orphan vec rows cleaned.
            assert (
                idx.db.execute(
                    "SELECT 1 FROM embed_state WHERE memory_id = ?",
                    (str(memory.id),),
                ).fetchone()
                is None
            )
            assert (
                idx.db.execute(
                    "SELECT 1 FROM memories_vec WHERE memory_id = ?",
                    (str(memory.id),),
                ).fetchone()
                is None
            )
        finally:
            idx.close()

    def test_record_embed_state_swallows_fk_when_raised_as_database_error(
        self, tmp_path: Path
    ) -> None:
        """The FK race handler must catch the violation regardless of
        whether SQLite surfaces it as :class:`sqlite3.IntegrityError`
        (the usual case) or as the parent
        :class:`sqlite3.DatabaseError` (observed on Ari's Python 3.12 +
        SQLite 3.45 deployment after the original PR #78 fix shipped —
        the narrow ``except IntegrityError`` clause didn't fire and the
        embed worker crashed).

        We swap ``idx._db`` for a wrapper that proxies everything to
        the real connection except ``execute`` of the ``INSERT INTO
        embed_state`` statement, which raises a bare
        :class:`sqlite3.DatabaseError` carrying the same FK message
        SQLite emits. The handler must still match by extended error
        code / message and run cleanup.
        """
        import sqlite3

        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="db-error-shape")
            idx.upsert(memory)
            # Plant an orphan vec row so the handler has something to clean.
            idx.upsert_vectors(str(memory.id), ["chunk"], [[0.3] * 8])
            idx.delete(str(memory.id))

            assert idx._db is not None
            real_db: sqlite3.Connection = idx._db
            insert_attempts: list[str] = []

            class _RaisingDB:
                """Proxy that intercepts the embed_state INSERT and
                raises ``sqlite3.DatabaseError`` (parent class), the
                shape Ari's box surfaced. Everything else falls
                through to the real connection."""

                def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
                    if "INSERT INTO embed_state" in sql:
                        insert_attempts.append(sql)
                        raise sqlite3.DatabaseError("FOREIGN KEY constraint failed")
                    return real_db.execute(sql, parameters)  # type: ignore[arg-type]

                def __getattr__(self, name: str) -> object:
                    return getattr(real_db, name)

                def __enter__(self) -> _RaisingDB:
                    real_db.__enter__()
                    return self

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc_val: BaseException | None,
                    exc_tb: object,
                ) -> object:
                    return real_db.__exit__(exc_type, exc_val, exc_tb)  # type: ignore[arg-type]

            idx._db = _RaisingDB()  # type: ignore[assignment]
            try:
                # Pre-fix this re-raised DatabaseError; post-fix the
                # handler matches by message/errorcode and swallows it.
                idx.record_embed_state(str(memory.id), body_hash("db-error-shape"), "sig:8")
            finally:
                idx._db = real_db

            assert insert_attempts, "test setup failed: INSERT was never attempted"

            # Orphan vec rows cleaned despite the non-IntegrityError
            # exception class — the handler still ran the cleanup branch.
            assert (
                idx.db.execute(
                    "SELECT 1 FROM memories_vec WHERE memory_id = ?",
                    (str(memory.id),),
                ).fetchone()
                is None
            ), "orphan vec rows leaked: handler didn't run on DatabaseError"
        finally:
            idx.close()

    def test_record_embed_state_propagates_non_fk_database_errors(self, tmp_path: Path) -> None:
        """Counterpart to the FK swallow test: the broadened catch must
        NOT swallow unrelated :class:`sqlite3.DatabaseError`s. A
        corruption error or schema mismatch should still propagate so
        the operator notices, instead of being silently logged as a
        race recovery."""
        import sqlite3

        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="non-fk-error")
            idx.upsert(memory)

            assert idx._db is not None
            real_db: sqlite3.Connection = idx._db

            class _CorruptDB:
                def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
                    if "INSERT INTO embed_state" in sql:
                        raise sqlite3.DatabaseError("database disk image is malformed")
                    return real_db.execute(sql, parameters)  # type: ignore[arg-type]

                def __getattr__(self, name: str) -> object:
                    return getattr(real_db, name)

                def __enter__(self) -> _CorruptDB:
                    real_db.__enter__()
                    return self

                def __exit__(
                    self,
                    exc_type: type[BaseException] | None,
                    exc_val: BaseException | None,
                    exc_tb: object,
                ) -> object:
                    return real_db.__exit__(exc_type, exc_val, exc_tb)  # type: ignore[arg-type]

            idx._db = _CorruptDB()  # type: ignore[assignment]
            try:
                with pytest.raises(sqlite3.DatabaseError, match="malformed"):
                    idx.record_embed_state(str(memory.id), body_hash("non-fk-error"), "sig:8")
            finally:
                idx._db = real_db
        finally:
            idx.close()


class TestBackfillEmbedState:
    """On schema v3 upgrade, memories that already have vectors should
    get `embed_state` rows written so the daemon doesn't re-enqueue
    everything on first start.

    ADR 0014 moved the call site from "every connect" to "exactly once
    when the recorded schema_version crosses v8". The v3-upgrade
    behavior is unchanged for legacy installs; the difference is that
    a vault already at v8 will not re-run the backfill on reopen. The
    setup helper below simulates a pre-v8 install by stamping
    `schema_version = 7` so the migration loop bumps it to 8 and runs
    the backfill exactly the way it would on a real upgrade.
    """

    @staticmethod
    def _stamp_pre_v8(db_path: Path) -> None:
        """Force schema_version back to 7 on disk so the next open
        triggers the v8 boundary backfill — the legacy-upgrade path
        ADR 0014 specifies."""
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (7)")
            conn.commit()
        finally:
            conn.close()

    def test_backfill_populates_state_for_vectorized_memories(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="legacy body")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["legacy body"], [[0.1] * 8])
            # Wipe embed_state to simulate the pre-v3 state where this
            # table doesn't exist yet.
            idx.db.execute("DELETE FROM embed_state")
            idx.db.commit()
        finally:
            idx.close()
        self._stamp_pre_v8(db_path)

        # Reopen — backfill runs in `_migrate` because old_current < 8.
        idx2 = Index(db_path, dimensions=8)
        idx2.connect()
        try:
            row = idx2.db.execute(
                "SELECT body_hash, embed_signature FROM embed_state WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            assert row is not None
            assert row["body_hash"] == body_hash("legacy body")
            # Backfill leaves signature NULL — we don't know what
            # produced the vectors, so we shouldn't claim to.
            assert row["embed_signature"] is None
        finally:
            idx2.close()

    def test_backfill_skips_memories_without_vectors(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="never embedded")
            idx.upsert(memory)
            # No vectors written. Wipe embed_state and reconnect.
            idx.db.execute("DELETE FROM embed_state")
            idx.db.commit()
        finally:
            idx.close()
        self._stamp_pre_v8(db_path)

        idx2 = Index(db_path, dimensions=8)
        idx2.connect()
        try:
            row = idx2.db.execute(
                "SELECT 1 FROM embed_state WHERE memory_id = ?", (str(memory.id),)
            ).fetchone()
            # No backfill — this memory was never embedded.
            assert row is None
        finally:
            idx2.close()

    def test_backfill_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="x")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["x"], [[0.1] * 8])
            # Manually record a real signature so we can verify backfill
            # doesn't clobber it on reopen.
            idx.record_embed_state(str(memory.id), body_hash("x"), "real:sig:8")
        finally:
            idx.close()
        self._stamp_pre_v8(db_path)

        idx2 = Index(db_path, dimensions=8)
        idx2.connect()
        try:
            row = idx2.db.execute(
                "SELECT embed_signature FROM embed_state WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            # Real signature preserved, NOT overwritten with NULL.
            assert row["embed_signature"] == "real:sig:8"
        finally:
            idx2.close()

    def test_backfill_does_not_run_on_already_migrated_db(self, tmp_path: Path) -> None:
        """ADR 0014 regression: a v8+ install must not re-run the
        backfill SELECT on reopen. Pre-fix this query did a vec0 scan
        per memory; on a 1+ GB index that's 30+ seconds of CPU on every
        CLI invocation. We assert the function isn't called by patching
        it and checking call count.
        """
        from unittest.mock import patch

        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="already-backfilled")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["already-backfilled"], [[0.1] * 8])
        finally:
            idx.close()

        # First connect bumped to v8. Reopen and verify the backfill
        # function is NOT called.
        idx2 = Index(db_path, dimensions=8)
        with patch.object(Index, "_backfill_embed_state", autospec=True) as mocked_backfill:
            idx2.connect()
            try:
                assert mocked_backfill.call_count == 0
            finally:
                idx2.close()

    def test_backfill_fast_path_skips_vec0_when_state_is_full(self, tmp_path: Path) -> None:
        """Defensive fast-path: even if `_backfill_embed_state` is
        called directly (bypassing the migration gate), it must not
        touch the vec0 table when every memory already has an
        embed_state row. The pre-check uses the embed_state PK index
        only, so the heavy SELECT against vec0 is never issued.

        We capture executed SQL via SQLite's trace callback and verify
        that no statement against `memories_vec` runs after the
        pre-check returns "nothing to backfill".
        """
        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="fully-stamped")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["fully-stamped"], [[0.1] * 8])
            idx.record_embed_state(str(memory.id), body_hash("fully-stamped"), "sig:8")

            executed: list[str] = []
            idx.db.set_trace_callback(executed.append)
            try:
                idx._backfill_embed_state()
            finally:
                idx.db.set_trace_callback(None)

            # The pre-check SELECT runs (and returns None). The heavy
            # SELECT that references memories_vec must not run.
            assert any("NOT EXISTS" in sql and "embed_state" in sql for sql in executed)
            assert not any("memories_vec" in sql for sql in executed), (
                f"vec0 was queried despite empty backfill set: {executed!r}"
            )
        finally:
            idx.close()

    def test_backfill_survives_duplicate_insert(self, tmp_path: Path) -> None:
        """Regression: the helper's INSERT must use `INSERT OR IGNORE`
        so that a duplicate `memory_id` row (from a competing connection
        that landed its INSERT after our SELECT but before ours) does
        not raise `UNIQUE constraint failed: embed_state.memory_id`.

        We force the conflicting state by inserting a row directly
        after the SELECT phase has captured its view, then driving the
        helper's INSERT statement with the same payload. Pre-fix this
        raises `sqlite3.IntegrityError`; post-fix the duplicate is
        silently skipped.
        """
        from datetime import UTC, datetime

        db_path = tmp_path / "index.db"
        idx = Index(db_path, dimensions=8)
        idx.connect()
        try:
            memory = _make_memory(body="contested")
            idx.upsert(memory)
            idx.upsert_vectors(str(memory.id), ["contested"], [[0.1] * 8])
            # Wipe embed_state so a fresh backfill SELECT would return
            # this memory's row.
            idx.db.execute("DELETE FROM embed_state")
            idx.db.commit()

            # Capture what the helper's SELECT would see right now.
            rows = idx.db.execute(
                """
                SELECT m.id AS id, m.body AS body
                FROM memories m
                WHERE EXISTS (SELECT 1 FROM memories_vec v WHERE v.memory_id = m.id)
                  AND NOT EXISTS (SELECT 1 FROM embed_state s WHERE s.memory_id = m.id)
                """
            ).fetchall()
            assert len(rows) == 1

            # Simulate the competing connection: it ran the same SELECT,
            # got the same row, and committed its INSERT before we
            # could. Now our cached SELECT view is stale.
            idx.db.execute(
                """
                INSERT INTO embed_state(memory_id, body_hash,
                                        embed_signature, embedded_at)
                VALUES (?, ?, NULL, '2026-04-27T00:00:00+00:00')
                """,
                (str(memory.id), body_hash("contested")),
            )
            idx.db.commit()

            # Drive the exact INSERT statement `_backfill_embed_state`
            # uses with the stale payload. With OR IGNORE this is a
            # no-op for the conflicting row; without it, IntegrityError.
            now = datetime.now(tz=UTC).isoformat()
            payload = [(r["id"], body_hash(r["body"]), None, now) for r in rows]
            idx.db.executemany(
                """
                INSERT OR IGNORE INTO embed_state(memory_id, body_hash,
                                                  embed_signature, embedded_at)
                VALUES (?, ?, ?, ?)
                """,
                payload,
            )
            idx.db.commit()

            # Exactly one row — the competitor's. The helper's stale
            # INSERT was ignored.
            count = idx.db.execute("SELECT COUNT(*) FROM embed_state").fetchone()[0]
            assert count == 1
        finally:
            idx.close()

    def test_backfill_uses_or_ignore_sql(self) -> None:
        """Source-level guard: the helper's SQL must literally contain
        `INSERT OR IGNORE`. If a future refactor strips it, the race
        regression returns silently — this assert catches that."""
        import inspect

        src = inspect.getsource(Index._backfill_embed_state)
        assert "INSERT OR IGNORE INTO embed_state" in src, (
            "_backfill_embed_state must use INSERT OR IGNORE; "
            "see test_backfill_survives_duplicate_insert for context"
        )


class TestQueryFts:
    def test_finds_match_in_body(self, index: Index) -> None:
        index.upsert(_make_memory(title="cloudflare", body="we use cloudflare for dns"))
        index.upsert(_make_memory(title="ollama", body="ollama serves embeddings"))
        hits = index.query_fts("cloudflare")
        assert len(hits) == 1
        assert hits[0].score < 0  # bm25 ranks are negative

    def test_finds_match_in_title(self, index: Index) -> None:
        index.upsert(_make_memory(title="deploy plan", body="unrelated"))
        hits = index.query_fts("deploy")
        assert len(hits) == 1

    def test_filters_by_type(self, index: Index) -> None:
        index.upsert(_make_memory(type_="memory", body="alpha"))
        index.upsert(
            _make_memory(
                type_="skill",
                title="alpha skill",
                body="alpha",
                scope="universal",
                verification="ok",
            )
        )
        memories_only = index.query_fts("alpha", types=["memory"])
        skills_only = index.query_fts("alpha", types=["skill"])
        assert len(memories_only) == 1
        assert len(skills_only) == 1
        assert memories_only[0].memory_id != skills_only[0].memory_id

    def test_no_match_returns_empty(self, index: Index) -> None:
        index.upsert(_make_memory(body="hello"))
        assert index.query_fts("nonexistent") == []


class TestQueryVec:
    def test_returns_nearest(self, index: Index) -> None:
        m1 = _make_memory(body="m1")
        m2 = _make_memory(body="m2")
        index.upsert(m1)
        index.upsert(m2)
        vec1 = _fake_embedding(1)
        vec2 = _fake_embedding(2)
        index.upsert_vectors(str(m1.id), ["c"], [vec1])
        index.upsert_vectors(str(m2.id), ["c"], [vec2])

        hits = index.query_vec(vec1, limit=2)
        assert hits[0].memory_id == str(m1.id)
        assert hits[1].memory_id == str(m2.id)
        assert hits[0].distance < hits[1].distance

    def test_dimension_mismatch_rejected(self, index: Index) -> None:
        with pytest.raises(ValueError, match="query embedding dim"):
            index.query_vec([0.1, 0.2, 0.3])

    def test_filters_by_type(self, index: Index) -> None:
        m1 = _make_memory(type_="memory", body="m")
        m2 = _make_memory(
            type_="skill",
            title="s",
            body="s",
            scope="universal",
            verification="ok",
        )
        index.upsert(m1)
        index.upsert(m2)
        vec1 = _fake_embedding(1)
        vec2 = _fake_embedding(2)
        index.upsert_vectors(str(m1.id), ["c"], [vec1])
        index.upsert_vectors(str(m2.id), ["c"], [vec2])

        memories_only = index.query_vec(vec1, limit=5, types=["memory"])
        assert all(h.memory_id == str(m1.id) for h in memories_only)

    def test_type_filter_with_multi_chunk_memories(self, index: Index) -> None:
        """Regression: when type-filtering, ``query_vec`` builds the
        ``IN (?,?,...)`` placeholder list from a dedupe set but passed
        the raw non-deduped list as bindings. A memory with multiple
        chunks produced fewer placeholders than parameters and crashed
        with::

            sqlite3.ProgrammingError: Incorrect number of bindings
                supplied. The current statement uses N, and there are
                M supplied.

        Observed in production logs as
        ``vec query failed; falling back to BM25: ... 63 ... 125``.
        """
        m1 = _make_memory(body="alpha")
        index.upsert(m1)
        # Give m1 three chunks so the vec over-fetch returns three rows
        # for the same memory_id. Over-fetch in `query_vec` is
        # `limit * 5` for type-filtered queries.
        index.upsert_vectors(
            str(m1.id),
            ["c1", "c2", "c3"],
            [_fake_embedding(1), _fake_embedding(2), _fake_embedding(3)],
        )

        hits = index.query_vec(_fake_embedding(1), limit=2, types=["memory"])
        assert all(h.memory_id == str(m1.id) for h in hits)


class TestConnectRequired:
    def test_db_property_raises_before_connect(self, tmp_path: Path) -> None:
        idx = Index(tmp_path / "index.db")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = idx.db


class TestThreadSafety:
    """The embed worker calls Index methods from `asyncio.to_thread`,
    which means SQLite ops happen on threads other than the one that
    opened the connection. PR #28 added an `RLock` around every read
    and write path so concurrent workers can't corrupt the connection's
    transaction state. These tests pound the index with 16 concurrent
    threads doing realistic mixes of upserts, vec writes, and queries
    — without the lock they hit `cannot commit - no transaction is
    active` and `bad parameter or other API misuse` within ~10 ops."""

    def test_concurrent_upserts_no_errors(self, index: Index) -> None:
        n = 50
        memories = [_make_memory(title=f"m{i}", body=f"body {i}") for i in range(n)]
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(m: Memory) -> None:
            try:
                index.upsert(m)
                index.upsert_vectors(
                    str(m.id), ["c"], [_fake_embedding(int(str(m.id)[:8], 16) % 1000)]
                )
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(worker, m) for m in memories]
            for f in as_completed(futs):
                f.result()
        assert errors == [], f"thread errors: {errors[:3]}"

        rows = index.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
        assert rows["c"] == n

    def test_concurrent_queue_ops_no_errors(self, index: Index) -> None:
        """Mixed enqueue / queue_pending / mark_embed_error from many threads."""
        n = 30
        memories = [_make_memory(title=f"q{i}") for i in range(n)]
        for m in memories:
            index.upsert(m)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def hammer(m: Memory) -> None:
            try:
                index.enqueue_embed(str(m.id))
                _ = index.queue_pending(limit=5)
                _ = index.queue_stats()
                index.mark_embed_error(str(m.id), "transient", max_retries=10)
                index.dequeue_embed(str(m.id))
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(hammer, m) for m in memories]
            for f in as_completed(futs):
                f.result()
        assert errors == [], f"thread errors: {errors[:3]}"
        # Every record was enqueued + dequeued, so the queue ends empty.
        assert index.queue_stats() == {"pending": 0, "failed": 0, "total": 0}
