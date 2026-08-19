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
