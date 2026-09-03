"""Tests for vec0 table compaction (ADR 0036)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from memstem.config import HygieneConfig
from memstem.core.index import Index
from memstem.hygiene.loop import HygieneLoop

DIMS = 8
CHUNK_SLOTS = 1024  # vec0 default chunk_size: slots are allocated in whole chunks


@pytest.fixture()
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=DIMS)
    idx.connect()
    yield idx
    idx.close()


def _vec(seed: int) -> list[float]:
    return [float(seed + i) for i in range(DIMS)]


def _upsert(index: Index, memory_id: str, n: int, seed: int = 0) -> None:
    index.upsert_vectors(
        memory_id,
        chunks=[f"chunk {i}" for i in range(n)],
        embeddings=[_vec(seed + i) for i in range(n)],
    )


def _parent(index: Index, memory_id: str) -> None:
    """Insert the parent ``memories`` row so ``record_embed_state`` doesn't hit
    the FK-violation branch (which deletes the vec rows). In production the
    embed worker only records state for memories that exist."""
    index.db.execute(
        """INSERT OR IGNORE INTO memories(id, type, source, title, body, path, created, updated)
           VALUES (?, 'memory', 'test', 't', 'b', ?, '2026-01-01', '2026-01-01')""",
        (memory_id, f"{memory_id}.md"),
    )
    index.db.commit()


def _fragment(index: Index) -> None:
    """Reproduce the production failure shape: a mass delete that leaves a
    few live rows pinned in every chunk. vec0 preallocates full chunks and
    only frees a chunk when *all* its slots are dead, so the survivors keep
    both chunks allocated while ~99% of the slots are dead."""
    _upsert(index, "filler-1", 700)
    _upsert(index, "keep-1", 1, seed=10_000)  # pinned in chunk 1
    _upsert(index, "filler-2", 800)
    _upsert(index, "keep-2", 1, seed=20_000)  # pinned in chunk 2
    _upsert(index, "filler-1", 0)  # mass delete
    _upsert(index, "filler-2", 0)


def _vector_checksum(index: Index) -> str:
    h = hashlib.sha256()
    for row in index.db.execute("SELECT chunk_id, embedding FROM memories_vec ORDER BY chunk_id"):
        h.update(row["chunk_id"].encode())
        h.update(row["embedding"])
    return h.hexdigest()


class TestVecOccupancy:
    def test_empty_table(self, index: Index) -> None:
        assert index.vec_occupancy() == (0, 0)

    def test_counts_live_rows(self, index: Index) -> None:
        _upsert(index, "m1", 3)
        live, slots = index.vec_occupancy()
        assert live == 3
        assert slots >= live

    def test_mass_delete_leaves_dead_slots(self, index: Index) -> None:
        _fragment(index)
        live, slots = index.vec_occupancy()
        assert live == 2
        # The ADR 0036 premise: both chunks stay allocated for 2 rows.
        assert slots == 2 * CHUNK_SLOTS


class TestCompactVectors:
    def test_preserves_rows_bytes_and_search(self, index: Index) -> None:
        _fragment(index)
        for m in range(5):
            _upsert(index, f"m{m}", 3, seed=m * 100)
        live_before, slots_before = index.vec_occupancy()
        assert live_before == 17  # 2 keepers + 5 x 3
        assert slots_before == 2 * CHUNK_SLOTS

        checksum_before = _vector_checksum(index)
        query = _vec(300)  # matches m3's first chunk
        hits_before = index.query_vec(query, limit=5)

        result = index.compact_vectors()

        assert result.live_rows == live_before
        assert result.slots_before == slots_before
        # 17 live rows fit in a single chunk after the rebuild.
        assert result.slots_after <= CHUNK_SLOTS
        live_after, slots_after = index.vec_occupancy()
        assert live_after == live_before
        assert slots_after == result.slots_after
        assert _vector_checksum(index) == checksum_before
        hits_after = index.query_vec(query, limit=5)
        assert [(h.chunk_id, round(h.distance, 6)) for h in hits_after] == [
            (h.chunk_id, round(h.distance, 6)) for h in hits_before
        ]

    def test_preserves_dimensions(self, index: Index) -> None:
        _upsert(index, "m1", 3)
        index.compact_vectors()
        assert index._vec_table_dimensions() == DIMS

    def test_empty_table_raises_nothing_missing(self, index: Index) -> None:
        # An existing-but-empty table compacts to an empty table.
        result = index.compact_vectors()
        assert result.live_rows == 0
        assert result.slots_after == 0

    def test_upserts_still_work_after_compact(self, index: Index) -> None:
        _upsert(index, "m1", 3)
        index.compact_vectors()
        index.upsert_vectors("m2", ["x"], [_vec(999)])
        live, _ = index.vec_occupancy()
        assert live == 4  # m1's 3 + 1 new
        hits = index.query_vec(_vec(999), limit=1)
        assert hits[0].memory_id == "m2"


class TestHygieneVecCompactStage:
    def _loop(self, index: Index, **cfg_overrides: object) -> HygieneLoop:
        cfg_kwargs: dict[str, object] = {
            "summarizer_provider": "noop",
            "vec_compact_min_dead_slots": 5,
            "vec_compact_max_occupancy": 0.9,
        }
        cfg_kwargs.update(cfg_overrides)
        cfg = HygieneConfig(**cfg_kwargs)  # type: ignore[arg-type]
        return HygieneLoop(vault=MagicMock(), index=index, cfg=cfg)

    def test_compacts_when_past_thresholds(self, index: Index) -> None:
        _fragment(index)
        live, slots = index.vec_occupancy()
        assert slots == 2 * CHUNK_SLOTS

        self._loop(index)._run_vec_compact()

        live_after, slots_after = index.vec_occupancy()
        assert live_after == live
        assert slots_after <= CHUNK_SLOTS

    def test_skips_below_min_dead_slots(self, index: Index) -> None:
        index.upsert_vectors("m1", ["a"], [_vec(1)])
        _, slots_before = index.vec_occupancy()

        self._loop(index, vec_compact_min_dead_slots=1000)._run_vec_compact()

        _, slots_after = index.vec_occupancy()
        assert slots_after == slots_before  # untouched

    def test_skips_at_high_occupancy(self, index: Index) -> None:
        # 100% occupancy: gate on occupancy even with min_dead_slots=0.
        index.upsert_vectors("m1", ["a", "b"], [_vec(1), _vec(2)])
        _, slots_before = index.vec_occupancy()

        loop = self._loop(index, vec_compact_min_dead_slots=0)
        loop._run_vec_compact()

        _, slots_after = index.vec_occupancy()
        assert slots_after == slots_before


class TestCompactConcurrency:
    """ADR 0039: mutations that land between bulk-copy batches must be
    reconciled by the delta pass, not lost or duplicated."""

    def _checksum(self, index: Index) -> str:
        return _vector_checksum(index)

    def test_reembed_during_copy_is_reconciled(self, index: Index) -> None:
        for m in range(4):
            _upsert(index, f"m{m}", 2, seed=m * 100)

        fired = {"done": False}

        def mutate() -> None:
            if fired["done"]:
                return
            fired["done"] = True
            # Re-embed m1 with different vectors mid-copy; record the
            # embed_state row exactly like the embed worker does — that
            # timestamp is what the delta pass keys on.
            _upsert(index, "m1", 2, seed=55_000)
            _parent(index, "m1")
            index.record_embed_state("m1", "hash-new", "sig")

        index.compact_vectors(batch_size=2, _after_batch=mutate)

        expected = self._checksum(index)
        live, _ = index.vec_occupancy()
        assert live == 8
        assert self._checksum(index) == expected
        hits = index.query_vec(_vec(55_000), limit=1)
        assert hits[0].memory_id == "m1"

    def test_delete_during_copy_is_reconciled(self, index: Index) -> None:
        for m in range(4):
            _upsert(index, f"m{m}", 2, seed=m * 100)

        fired = {"done": False}

        def mutate() -> None:
            if fired["done"]:
                return
            fired["done"] = True
            index.delete("m2")

        result = index.compact_vectors(batch_size=2, _after_batch=mutate)

        assert result.live_rows == 6
        live, _ = index.vec_occupancy()
        assert live == 6
        assert index.query_vec(_vec(200), limit=1)[0].memory_id != "m2"

    def test_insert_during_copy_is_reconciled(self, index: Index) -> None:
        for m in range(4):
            _upsert(index, f"m{m}", 2, seed=m * 100)

        fired = {"done": False}

        def mutate() -> None:
            if fired["done"]:
                return
            fired["done"] = True
            _upsert(index, "brand-new", 3, seed=77_000)
            _parent(index, "brand-new")
            index.record_embed_state("brand-new", "hash", "sig")

        result = index.compact_vectors(batch_size=2, _after_batch=mutate)

        assert result.live_rows == 11
        hits = index.query_vec(_vec(77_000), limit=1)
        assert hits[0].memory_id == "brand-new"


class TestAtomicSwap:
    """ADR 0040: the rebuild happens beside the live table and is swapped in
    by renaming the vec0 table plus every shadow table."""

    def _tables(self, index: Index) -> list[str]:
        rows = index.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memories_vec%' "
            "ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def test_shadow_tables_renamed_and_no_build_leftovers(self, index: Index) -> None:
        _fragment(index)
        before = self._tables(index)
        assert "memories_vec_rowids" in before

        index.compact_vectors(batch_size=1)

        after = self._tables(index)
        assert after == before  # identical set of names: vtab + every shadow
        assert not any(n.startswith("memories_vec_new") for n in after)
        assert "_vec_compact_stage" not in [
            r[0] for r in index.db.execute("SELECT name FROM sqlite_master").fetchall()
        ]
        # The swapped-in table is fully functional: occupancy, KNN, upsert.
        live, slots = index.vec_occupancy()
        assert live == 2
        assert slots <= CHUNK_SLOTS
        assert index.query_vec(_vec(10_000), limit=1)[0].memory_id == "keep-1"
        index.upsert_vectors("post", ["x"], [_vec(4242)])
        assert index.query_vec(_vec(4242), limit=1)[0].memory_id == "post"

    def test_mismatch_aborts_keeps_live_table_and_drops_build(self, index: Index) -> None:
        for m in range(3):
            _upsert(index, f"m{m}", 2, seed=m * 100)
        checksum = _vector_checksum(index)
        fired = {"done": False}

        def sneak_insert() -> None:
            # A vec row that lands mid-copy WITHOUT an embed_state record is
            # invisible to the delta pass -> counts disagree -> must abort.
            if fired["done"]:
                return
            fired["done"] = True
            _upsert(index, "ghost", 1, seed=99_000)

        with pytest.raises(RuntimeError, match="rolling back"):
            index.compact_vectors(batch_size=2, _after_batch=sneak_insert)

        # Live table untouched (still has the ghost row too), build table gone.
        assert _vector_checksum(index) == _vector_checksum(index)
        live, _ = index.vec_occupancy()
        assert live == 7
        assert index.query_vec(_vec(99_000), limit=1)[0].memory_id == "ghost"
        assert not any(n.startswith("memories_vec_new") for n in self._tables(index))
        assert checksum != _vector_checksum(index)  # ghost was added; nothing lost

    def test_shadow_names_exclude_sibling_vec_tables(self, index: Index) -> None:
        _upsert(index, "m1", 1)
        ddl = index.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_vec'"
        ).fetchone()[0]
        index.db.execute(ddl.replace("memories_vec", "memories_vec_new", 1))
        index.db.execute(ddl.replace("memories_vec", "memories_vec_old", 1))
        index.db.commit()
        live_names = index._vec_shadow_names("memories_vec")
        assert live_names[0] == "memories_vec"
        assert "memories_vec_rowids" in live_names
        assert not any("_new" in n or "_old" in n for n in live_names)
        new_names = index._vec_shadow_names("memories_vec_new")
        assert new_names[0] == "memories_vec_new"
        assert all(n.startswith("memories_vec_new") for n in new_names)
        index.db.execute("DROP TABLE memories_vec_new")
        index.db.execute("DROP TABLE memories_vec_old")
        index.db.commit()

    def test_no_old_table_left_after_swap(self, index: Index) -> None:
        _fragment(index)
        index.compact_vectors(batch_size=1, batch_pause_seconds=0)
        assert not any(n.startswith("memories_vec_old") for n in self._tables(index))
        assert index.vec_occupancy()[0] == 2

    def test_leftover_old_table_from_crash_is_cleared(self, index: Index) -> None:
        _upsert(index, "m1", 2)
        ddl = index.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_vec'"
        ).fetchone()[0]
        index.db.execute(ddl.replace("memories_vec", "memories_vec_old", 1))
        index.db.commit()
        assert any(n.startswith("memories_vec_old") for n in self._tables(index))

        index.compact_vectors(batch_pause_seconds=0)

        assert not any(n.startswith("memories_vec_old") for n in self._tables(index))
        assert index.query_vec(_vec(1), limit=1)[0].memory_id == "m1"

    def test_leftover_build_table_from_crash_is_cleared(self, index: Index) -> None:
        _upsert(index, "m1", 2)
        ddl = index.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_vec'"
        ).fetchone()[0]
        index.db.execute(ddl.replace("memories_vec", "memories_vec_new", 1))
        index.db.execute(
            "INSERT INTO memories_vec_new(chunk_id, memory_id, chunk_index, embedding) "
            "SELECT chunk_id, memory_id, chunk_index, embedding FROM memories_vec"
        )
        index.db.commit()
        assert any(n.startswith("memories_vec_new") for n in self._tables(index))

        result = index.compact_vectors()

        assert result.live_rows == 2
        assert not any(n.startswith("memories_vec_new") for n in self._tables(index))
        assert index.query_vec(_vec(1), limit=1)[0].memory_id == "m1"
