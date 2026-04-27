"""Tests for ADR 0012 PR-A: exact-body hash dedup."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.dedup import (
    find_existing_memory_for_hash,
    increment_seen_count,
    normalized_body_hash,
    record_body_hash,
)
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault


def _record(
    body: str = "Some legitimate memory content.",
    *,
    source: str = "openclaw",
    ref: str = "/tmp/test.md",
) -> MemoryRecord:
    metadata: dict[str, Any] = {
        "type": "memory",
        "created": "2026-04-27T10:00:00+00:00",
        "updated": "2026-04-27T10:00:00+00:00",
    }
    return MemoryRecord(
        source=source,
        ref=ref,
        title="Test",
        body=body,
        tags=[],
        metadata=metadata,
    )


def _processed(pipe: Pipeline, record: MemoryRecord) -> Memory:
    memory = pipe.process(record)
    assert memory is not None, "pipeline unexpectedly returned None"
    return memory


# --- normalized_body_hash ---


class TestNormalizedBodyHash:
    def test_same_body_same_hash(self) -> None:
        assert normalized_body_hash("foo") == normalized_body_hash("foo")

    def test_different_body_different_hash(self) -> None:
        assert normalized_body_hash("foo") != normalized_body_hash("bar")

    def test_whitespace_runs_collapsed(self) -> None:
        assert normalized_body_hash("foo bar") == normalized_body_hash("foo   bar")
        assert normalized_body_hash("foo bar") == normalized_body_hash("foo\t\tbar")
        assert normalized_body_hash("foo bar") == normalized_body_hash("foo\n\nbar")

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert normalized_body_hash("foo") == normalized_body_hash("  foo  ")
        assert normalized_body_hash("foo") == normalized_body_hash("\n\nfoo\n\n")

    def test_case_insensitive(self) -> None:
        assert normalized_body_hash("Hello") == normalized_body_hash("HELLO")
        assert normalized_body_hash("FooBar") == normalized_body_hash("foobar")

    def test_empty_body(self) -> None:
        # Empty / whitespace-only bodies all hash to the same value (the SHA-256
        # of the empty string). This is fine — empty bodies aren't dedup
        # targets anyway; the noise filter and downstream layers handle them.
        assert normalized_body_hash("") == normalized_body_hash("   \n\t  ")

    def test_returns_hex_string(self) -> None:
        h = normalized_body_hash("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# --- pipeline integration ---


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


class TestPipelineDedup:
    def test_first_emit_persists_normally(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="Original content"))
        assert memory.body == "Original content"
        # The hash row was recorded.
        rows = index.db.execute("SELECT memory_id, seen_count FROM body_hash_index").fetchall()
        assert len(rows) == 1
        assert rows[0]["memory_id"] == str(memory.id)
        assert rows[0]["seen_count"] == 1

    def test_idempotent_re_emit_same_ref_same_body(self, vault: Vault, index: Index) -> None:
        # Re-emitting the same (source, ref) with unchanged body should NOT
        # be deduplicated. It's the same memory updating itself.
        pipe = Pipeline(vault, index)
        first = _processed(pipe, _record(body="hello", ref="/x.md"))
        second = _processed(pipe, _record(body="hello", ref="/x.md"))
        assert first.id == second.id
        # The hash row's seen_count is bumped on re-emit.
        rows = index.db.execute("SELECT seen_count FROM body_hash_index").fetchall()
        assert len(rows) == 1
        assert rows[0]["seen_count"] == 2

    def test_cross_record_duplicate_is_dropped(
        self, vault: Vault, index: Index, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Two different (source, ref) pairs with identical body bodies →
        # the second one is dropped, the first wins, seen_count bumped.
        pipe = Pipeline(vault, index)
        first = _processed(pipe, _record(body="shared content", ref="/a.md"))
        with caplog.at_level("INFO", logger="memstem.core.pipeline"):
            second = pipe.process(_record(body="shared content", ref="/b.md"))
        assert second is None
        # First memory still exists, hash count bumped.
        rows = index.db.execute("SELECT memory_id, seen_count FROM body_hash_index").fetchall()
        assert len(rows) == 1
        assert rows[0]["memory_id"] == str(first.id)
        assert rows[0]["seen_count"] == 2
        assert any("dedup" in r.message.lower() for r in caplog.records)

    def test_dedup_normalizes_whitespace_and_case(self, vault: Vault, index: Index) -> None:
        # Trivial formatting differences must not bypass dedup.
        pipe = Pipeline(vault, index)
        _processed(pipe, _record(body="Hello  World", ref="/a.md"))
        result = pipe.process(_record(body="hello world", ref="/b.md"))
        assert result is None

    def test_distinct_bodies_both_persisted(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        a = _processed(pipe, _record(body="first body", ref="/a.md"))
        b = _processed(pipe, _record(body="second body", ref="/b.md"))
        assert a.id != b.id
        rows = index.db.execute("SELECT body_hash FROM body_hash_index").fetchall()
        assert len(rows) == 2

    def test_body_change_cleans_up_old_hash(self, vault: Vault, index: Index) -> None:
        # When a memory's body changes (file edited), the OLD hash should
        # not linger and false-positive a future record bearing the old body.
        pipe = Pipeline(vault, index)
        _processed(pipe, _record(body="version 1", ref="/x.md"))
        _processed(pipe, _record(body="version 2", ref="/x.md"))
        # Only the new hash should remain.
        rows = index.db.execute("SELECT body_hash FROM body_hash_index").fetchall()
        assert len(rows) == 1

        # The old body, if it appeared again under a new ref, must NOT dedup
        # (the old hash row was cleaned up).
        result = _processed(pipe, _record(body="version 1", ref="/y.md"))
        assert result.body == "version 1"

    def test_hash_row_cascades_on_memory_delete(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="to be deleted", ref="/x.md"))
        # Sanity check: the hash row exists.
        assert (
            index.db.execute(
                "SELECT 1 FROM body_hash_index WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            is not None
        )
        # Delete the memory directly via the index.
        with index.db:
            index.db.execute("DELETE FROM memories WHERE id = ?", (str(memory.id),))
        # The hash row is gone via ON DELETE CASCADE.
        assert (
            index.db.execute(
                "SELECT 1 FROM body_hash_index WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            is None
        )


# --- helpers (direct unit tests) ---


class TestHelpers:
    def test_find_returns_none_for_missing_hash(self, vault: Vault, index: Index) -> None:
        assert find_existing_memory_for_hash(index.db, "deadbeef" * 8) is None

    def test_record_then_find_round_trip(self, vault: Vault, index: Index) -> None:
        # Set up a real memory (so the FK is satisfiable) by going through
        # the pipeline once.
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="hello", ref="/x.md"))
        h = normalized_body_hash("hello")
        assert find_existing_memory_for_hash(index.db, h) == str(memory.id)

    def test_increment_seen_count_bumps_counter(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        _processed(pipe, _record(body="x", ref="/x.md"))
        h = normalized_body_hash("x")
        with index.db:
            increment_seen_count(index.db, h)
            increment_seen_count(index.db, h)
        row = index.db.execute(
            "SELECT seen_count FROM body_hash_index WHERE body_hash = ?", (h,)
        ).fetchone()
        # 1 from initial record_body_hash, +2 from increments.
        assert row["seen_count"] == 3

    def test_record_body_hash_cleans_stale_entries(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="old body", ref="/x.md"))
        # Manually invoke record_body_hash with a new hash for the same memory.
        new_hash = normalized_body_hash("new body")
        with index.db:
            record_body_hash(index.db, new_hash, str(memory.id))
        # Only the new hash should exist for this memory.
        rows = index.db.execute(
            "SELECT body_hash FROM body_hash_index WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["body_hash"] == new_hash
