"""One vec0 row per distinct vector; equal vectors are aliases (ADR 0056)."""

from __future__ import annotations

import hashlib
import math
import random
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from memstem.core import index as index_mod
from memstem.core.index import Index

DIMS = 8


@pytest.fixture()
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=DIMS)
    idx.connect()
    yield idx
    idx.close()


def _vec(seed: int) -> list[float]:
    return [float(seed + i) for i in range(DIMS)]


def _blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _parent(index: Index, memory_id: str) -> None:
    index.db.execute(
        """INSERT OR IGNORE INTO memories(id, type, source, title, body, path, created, updated)
           VALUES (?, 'memory', 'test', 't', 'b', ?, '2026-01-01', '2026-01-01')""",
        (memory_id, f"{memory_id}.md"),
    )
    index.db.commit()


def _upsert(index: Index, memory_id: str, vecs: list[list[float]]) -> None:
    index.upsert_vectors(memory_id, chunks=[f"c{i}" for i in range(len(vecs))], embeddings=vecs)


def _counts(index: Index) -> tuple[int, int]:
    rows = index.db.execute("SELECT count(*) FROM memories_vec_rowids").fetchone()[0]
    return int(rows), index.vec_alias_count()


def _all_chunks(index: Index) -> dict[str, bytes]:
    """chunk_id -> vector for every chunk, canonical or alias."""
    out = {
        r[0]: bytes(r[1]) for r in index.db.execute("SELECT chunk_id, embedding FROM memories_vec")
    }
    for cid, canonical in index.db.execute(
        "SELECT chunk_id, canonical_chunk_id FROM vec_aliases"
    ).fetchall():
        out[cid] = out[canonical]
    return out


def _legacy_insert(index: Index, memory_id: str, vecs: list[list[float]]) -> None:
    """Rows written the pre-0056 way: one vec0 row per chunk, no dedupe."""
    for i, v in enumerate(vecs):
        index.db.execute(
            "INSERT INTO memories_vec(chunk_id, memory_id, chunk_index, embedding) "
            "VALUES (?, ?, ?, ?)",
            (f"{memory_id}:{i}", memory_id, i, _blob(v)),
        )
    index.db.commit()


def _assert_consistent(index: Index) -> None:
    """Invariants: a chunk is canonical XOR alias; aliases point at live rows;
    no vec0 row duplicates another; vec_canon points at live, matching rows."""
    canonical = {
        r[0]: bytes(r[1]) for r in index.db.execute("SELECT chunk_id, embedding FROM memories_vec")
    }
    aliases = dict(
        index.db.execute("SELECT chunk_id, canonical_chunk_id FROM vec_aliases").fetchall()
    )
    assert not set(canonical) & set(aliases)
    assert all(c in canonical for c in aliases.values())
    assert len(set(canonical.values())) == len(canonical)
    for h, cid in index.db.execute("SELECT vec_hash, chunk_id FROM vec_canon"):
        assert cid in canonical
        assert hashlib.sha256(canonical[cid]).digest()[:16] == bytes(h)


class TestWriteDedupe:
    def test_equal_vectors_share_one_row(self, index: Index) -> None:
        _upsert(index, "a", [_vec(1), _vec(2)])
        _upsert(index, "b", [_vec(1), _vec(3)])
        assert _counts(index) == (3, 1)
        hits = index.query_vec(_vec(1), limit=10)
        assert {h.chunk_id for h in hits if h.distance == 0} == {"a:0", "b:0"}
        _assert_consistent(index)

    def test_repeated_chunk_within_one_record(self, index: Index) -> None:
        _upsert(index, "a", [_vec(1), _vec(1), _vec(1)])
        assert _counts(index) == (1, 2)
        _upsert(index, "a", [])
        assert _counts(index) == (0, 0)
        _assert_consistent(index)

    def test_deleting_canonical_promotes_an_alias(self, index: Index) -> None:
        for m in ("a", "b", "c"):
            _upsert(index, m, [_vec(1)])
        canonical = index.db.execute("SELECT chunk_id FROM memories_vec").fetchone()[0]
        assert canonical == "a:0"
        index.upsert_vectors("a", chunks=[], embeddings=[])
        assert _counts(index) == (1, 1)
        assert index.db.execute("SELECT chunk_id FROM memories_vec").fetchone()[0] == "b:0"
        hits = index.query_vec(_vec(1), limit=5)
        assert [h.chunk_id for h in hits] == ["b:0", "c:0"]
        # New copies dedupe against the promoted row.
        _upsert(index, "d", [_vec(1)])
        assert _counts(index) == (1, 2)
        _assert_consistent(index)

    def test_delete_record_cleans_aliases(self, index: Index) -> None:
        for m in ("a", "b"):
            _parent(index, m)
            _upsert(index, m, [_vec(1), _vec(7)])
        index.delete("b")
        assert _counts(index) == (2, 0)
        index.delete("a")
        assert _counts(index) == (0, 0)
        assert index.db.execute("SELECT count(*) FROM vec_canon").fetchone()[0] == 0

    def test_update_of_shared_canonical_keeps_alias_vector(self, index: Index) -> None:
        _parent(index, "a")
        _upsert(index, "a", [_vec(1)])
        _upsert(index, "b", [_vec(1)])
        index.apply_vectors("a", hashes=["h-new"], vectors={0: _vec(50)})
        chunks = _all_chunks(index)
        assert chunks["a:0"] == _blob(_vec(50))
        assert chunks["b:0"] == _blob(_vec(1))
        _assert_consistent(index)

    def test_update_to_an_existing_vector_becomes_alias(self, index: Index) -> None:
        _parent(index, "a")
        _upsert(index, "a", [_vec(5)])
        _upsert(index, "b", [_vec(1)])
        index.apply_vectors("a", hashes=["h"], vectors={0: _vec(1)})
        assert _counts(index) == (1, 1)
        _assert_consistent(index)

    def test_unshared_update_stays_in_place(self, index: Index) -> None:
        _parent(index, "a")
        _upsert(index, "a", [_vec(5)])
        rowid = index.db.execute("SELECT rowid FROM memories_vec_rowids").fetchone()[0]
        index.apply_vectors("a", hashes=["h"], vectors={0: _vec(6)})
        assert index.db.execute("SELECT rowid FROM memories_vec_rowids").fetchone()[0] == rowid
        _assert_consistent(index)

    def test_hash_collision_never_aliases_different_vectors(
        self, index: Index, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(index_mod, "_vec_hash", lambda blob: b"\x00" * 16)
        _upsert(index, "a", [_vec(1)])
        _upsert(index, "b", [_vec(2)])
        assert _counts(index) == (2, 0)

    def test_vector_blob_and_reuse_resolve_aliases(self, index: Index) -> None:
        _upsert(index, "a", [_vec(1)])
        _upsert(index, "b", [_vec(1)])
        assert index.vector_blob("b:0") == _blob(_vec(1))
        with index.reader() as rdb:
            assert rdb is not None
            assert index.vector_blob("b:0", db=rdb) == _blob(_vec(1))
        assert index.vector_blob("missing:0") is None


class TestSearchEquivalence:
    def test_top_k_matches_brute_force_by_distance(self, index: Index) -> None:
        rng = random.Random(56)
        pool = [[rng.uniform(-1, 1) for _ in range(DIMS)] for _ in range(15)]
        expected: list[tuple[str, list[float]]] = []
        for m in range(40):
            vecs = [pool[rng.randrange(len(pool))] for _ in range(rng.randint(1, 4))]
            _upsert(index, f"m{m:02d}", vecs)
            expected += [(f"m{m:02d}:{i}", v) for i, v in enumerate(vecs)]
        assert index.vec_alias_count() > 0
        query = [rng.uniform(-1, 1) for _ in range(DIMS)]

        def dist(v: list[float]) -> float:
            # float32 storage, so compare in float32.
            stored = struct.unpack(f"{DIMS}f", _blob(v))
            return math.sqrt(sum((a - b) ** 2 for a, b in zip(stored, query, strict=True)))

        for k in (1, 5, 17, 60, 500):
            hits = index.query_vec(query, limit=k)
            want = sorted(dist(v) for _, v in expected)[:k]
            assert len(hits) == len(want)
            assert [h.distance for h in hits] == pytest.approx(want, abs=1e-5)
            assert len({h.chunk_id for h in hits}) == len(hits)
            assert hits == sorted(hits, key=lambda h: h.distance)
        _assert_consistent(index)

    def test_type_filter_still_applies(self, index: Index) -> None:
        for m, t in (("a", "memory"), ("b", "skill")):
            index.db.execute(
                """INSERT INTO memories(id, type, source, title, body, path, created, updated)
                   VALUES (?, ?, 'test', 't', 'b', ?, '2026-01-01', '2026-01-01')""",
                (m, t, f"{m}.md"),
            )
            _upsert(index, m, [_vec(1)])
        index.db.commit()
        hits = index.query_vec(_vec(1), limit=5, types=["skill"])
        assert [h.memory_id for h in hits] == ["b"]


class TestDedupePass:
    def _legacy_vault(self, index: Index) -> dict[str, bytes]:
        _legacy_insert(index, "a", [_vec(1), _vec(2), _vec(1)])
        _legacy_insert(index, "b", [_vec(2), _vec(3)])
        _legacy_insert(index, "c", [_vec(1)])
        return _all_chunks(index)

    def test_converts_duplicates_and_keeps_every_chunk(self, index: Index) -> None:
        before = self._legacy_vault(index)
        assert _counts(index) == (6, 0)
        seen: list[tuple[int, int]] = []
        result = index.dedupe_vectors(
            batch_size=2, batch_pause_seconds=0, progress=lambda s, c: seen.append((s, c))
        )
        assert (result.scanned, result.changed) == (6, 3)
        assert (result.canonical_rows, result.aliases) == (3, 3)
        assert seen[-1] == (6, 3)
        assert _all_chunks(index) == before
        _assert_consistent(index)
        again = index.dedupe_vectors(batch_pause_seconds=0)
        assert again.changed == 0

    def test_search_results_unchanged(self, index: Index) -> None:
        self._legacy_vault(index)
        query = [0.5 + i for i in range(DIMS)]
        before = index.query_vec(query, limit=10)
        index.dedupe_vectors(batch_pause_seconds=0)
        after = index.query_vec(query, limit=10)
        assert [h.distance for h in after] == pytest.approx([h.distance for h in before])
        assert {h.chunk_id for h in after} == {h.chunk_id for h in before}

    def test_compaction_after_dedupe_keeps_aliases(self, index: Index) -> None:
        before = self._legacy_vault(index)
        index.dedupe_vectors(batch_pause_seconds=0)
        index.compact_vectors(batch_pause_seconds=0)
        assert _all_chunks(index) == before
        _assert_consistent(index)

    def test_undo_restores_one_row_per_chunk(self, index: Index) -> None:
        before = self._legacy_vault(index)
        index.dedupe_vectors(batch_pause_seconds=0)
        result = index.undo_vector_dedupe(batch_size=2)
        assert (result.changed, result.aliases, result.canonical_rows) == (3, 0, 6)
        rows = {
            r[0]: bytes(r[1])
            for r in index.db.execute("SELECT chunk_id, embedding FROM memories_vec")
        }
        assert rows == before

    def test_dedupe_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from memstem import cli

        idx = Index(tmp_path / "index.db", dimensions=DIMS)
        idx.connect()
        _legacy_insert(idx, "a", [_vec(1), _vec(1)])
        idx.close()

        def _open(cfg: object) -> Index:
            i = Index(tmp_path / "index.db", dimensions=DIMS)
            i.connect()
            return i

        monkeypatch.setattr(cli, "_load_config", lambda path: object())
        monkeypatch.setattr(cli, "_resolve_vault_path", lambda vault: tmp_path)
        monkeypatch.setattr(cli, "_open_index", _open)
        out = CliRunner().invoke(cli.app, ["vec-dedupe"])
        assert out.exit_code == 0, out.output
        assert "1 became aliases" in out.output
        out = CliRunner().invoke(cli.app, ["vec-dedupe", "--undo"])
        assert out.exit_code == 0, out.output
        assert "1 aliases restored" in out.output
