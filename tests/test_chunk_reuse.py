"""Per-chunk embedding reuse and in-place vector writes (ADR 0045)."""

from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.embed_worker import EmbedWorker
from memstem.core.embeddings import Embedder
from memstem.core.index import Index, StaleVectorPlanError, VectorReusePlan
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault

DIMS = 8
# Each paragraph is > half the 2048-char chunk limit, so chunk_text packs
# exactly one paragraph per chunk and the tests can reason per paragraph.
PARA_LEN = 1100


def _para(tag: str) -> str:
    return (tag + " ") * (PARA_LEN // (len(tag) + 1))


def _vec_for(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest[:DIMS]]


class _ContentEmbedder(Embedder):
    """Vector derived from the text, so a reused vector is checkable."""

    dimensions = DIMS

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _embed_batch(self, texts: list[str], timeout: float) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_vec_for(t) for t in texts]

    @property
    def embedded(self) -> list[str]:
        return [t for call in self.calls for t in call]


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=DIMS)
    idx.connect()
    yield idx
    idx.close()


class _Harness:
    def __init__(self, vault: Vault, index: Index, signature: str = "p:m:8") -> None:
        self.vault, self.index = vault, index
        self.pipe = Pipeline(vault, index)
        self.ref = f"/tmp/{uuid4()}.md"
        self.embedder = _ContentEmbedder()
        self.signature = signature
        self.memory_id = ""

    def write(self, paragraphs: list[str]) -> None:
        record = MemoryRecord(
            source="test",
            ref=self.ref,
            title="t",
            body="\n\n".join(paragraphs),
            tags=[],
            metadata={
                "type": "memory",
                "created": "2026-09-26T00:00:00+00:00",
                "updated": "2026-09-26T00:00:00+00:00",
            },
        )
        memory = self.pipe.process(record)
        assert memory is not None
        self.memory_id = str(memory.id)

    def embed(self, signature: str | None = None) -> list[str]:
        """Drain the queue; returns the texts sent to the embedder."""
        before = len(self.embedder.embedded)
        worker = EmbedWorker(
            vault=self.vault,
            index=self.index,
            embedder=self.embedder,
            batch_size=10,
            idle_sleep=0,
            embedding_signature=signature or self.signature,
        )
        while asyncio.run(worker.tick()):
            pass
        return self.embedder.embedded[before:]

    def stored(self) -> dict[int, list[float]]:
        out = {}
        for cid in self.index._vec_chunk_ids(self.memory_id):
            blob = self.index.db.execute(
                "SELECT embedding FROM memories_vec WHERE chunk_id = ?", (cid,)
            ).fetchone()[0]
            out[int(cid.rsplit(":", 1)[1])] = list(struct.unpack(f"{DIMS}f", blob))
        return out

    def max_rowid(self) -> int:
        return int(
            self.index.db.execute("SELECT max(rowid) FROM memories_vec_rowids").fetchone()[0]
        )

    def assert_vectors_match(self, paragraphs: list[str]) -> None:
        stored = self.stored()
        assert sorted(stored) == list(range(len(paragraphs)))
        for i, p in enumerate(paragraphs):
            assert stored[i] == pytest.approx(_vec_for(p.strip()), abs=1e-6), f"chunk {i}"
        assert len(self.index.chunk_hashes(self.memory_id)) == len(paragraphs)


def test_append_embeds_only_new_chunks_and_allocates_only_for_them(
    vault: Vault, index: Index
) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(4)]
    h.write(paras)
    assert len(h.embed()) == 4
    rowid = h.max_rowid()

    paras.append(_para("p4"))
    h.write(paras)
    sent = h.embed()
    assert sent == [paras[4].strip()]
    h.assert_vectors_match(paras)
    # One new row; the four existing rows kept their slots.
    assert h.max_rowid() == rowid + 1


def test_edit_updates_rows_in_place(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()
    rowid = h.max_rowid()

    paras[1] = _para("changed")
    h.write(paras)
    assert h.embed() == [paras[1].strip()]
    h.assert_vectors_match(paras)
    assert h.max_rowid() == rowid  # UPDATE in place: no new slot


def test_moved_chunks_are_copied_not_embedded(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()

    paras.insert(0, _para("new-first"))
    h.write(paras)
    assert h.embed() == [paras[0].strip()]
    h.assert_vectors_match(paras)


def test_shrink_deletes_tail_rows_without_embedding(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(4)]
    h.write(paras)
    h.embed()

    paras = paras[:2]
    h.write(paras)
    assert h.embed() == []
    h.assert_vectors_match(paras)


def test_signature_change_embeds_everything_in_place(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()
    rowid = h.max_rowid()

    paras.append(_para("p3"))
    h.write(paras)
    assert len(h.embed(signature="other:model:8")) == 4
    h.assert_vectors_match(paras)
    assert h.max_rowid() == rowid + 1


def test_records_without_hashes_embed_fully(vault: Vault, index: Index) -> None:
    # Everything embedded before ADR 0045 has vectors but no hashes.
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()
    with index.lock, index.db:
        index.db.execute("DELETE FROM vec_chunk_hashes")
    rowid = h.max_rowid()

    paras.append(_para("p3"))
    h.write(paras)
    assert len(h.embed()) == 4
    h.assert_vectors_match(paras)
    assert h.max_rowid() == rowid + 1  # still in place


def test_hash_without_vector_row_is_a_miss(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()
    with index.lock, index.db:
        index.db.execute("DELETE FROM memories_vec WHERE chunk_id = ?", (f"{h.memory_id}:1",))

    paras.append(_para("p3"))
    h.write(paras)
    assert sorted(h.embed()) == sorted([paras[1].strip(), paras[3].strip()])
    h.assert_vectors_match(paras)


def test_stale_plan_releases_claim_without_spending_a_retry(
    vault: Vault, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(vault, index)
    h.write([_para("p0")])

    def stale(*a: object, **kw: object) -> tuple[int, int, int]:
        raise StaleVectorPlanError("moved")

    monkeypatch.setattr(index, "apply_vectors", stale)
    worker = EmbedWorker(vault=vault, index=index, embedder=h.embedder, batch_size=10, idle_sleep=0)
    assert asyncio.run(worker.tick()) == 0
    row = index.db.execute(
        "SELECT retry_count, claimed_by FROM embed_queue WHERE memory_id = ?", (h.memory_id,)
    ).fetchone()
    assert row is not None
    assert row["retry_count"] == 0
    assert row["claimed_by"] is None


def test_apply_vectors_detects_changed_unchanged_rows(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(2)]
    h.write(paras)
    h.embed()
    hashes = list(index.chunk_hashes(h.memory_id).values())
    with index.lock, index.db:
        index.db.execute(
            "UPDATE vec_chunk_hashes SET chunk_hash = 'x' WHERE memory_id = ? AND chunk_index = 0",
            (h.memory_id,),
        )
    with pytest.raises(StaleVectorPlanError):
        index.apply_vectors(h.memory_id, hashes, {}, frozenset({0, 1}))


def test_apply_vectors_rejects_missing_and_wrong_size_vectors(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    h.write([_para("p0")])
    with pytest.raises(ValueError, match="no vector"):
        index.apply_vectors(h.memory_id, ["a", "b"], {0: [0.0] * DIMS})
    with pytest.raises(ValueError, match="dim"):
        index.apply_vectors(h.memory_id, ["a"], {0: [0.0] * 3})
    with pytest.raises(ValueError, match="blob"):
        index.apply_vectors(h.memory_id, ["a"], {0: b"\x00" * 4})


def test_apply_vectors_on_deleted_memory_writes_nothing(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    h.write([_para("p0")])
    h.embed()
    mid = h.memory_id
    with index.lock, index.db:
        index.db.execute("DELETE FROM memories WHERE id = ?", (mid,))
    assert index.apply_vectors(mid, ["a"], {0: [0.0] * DIMS}) == (0, 0, 1)
    assert index._vec_chunk_ids(mid) == []
    assert index.chunk_hashes(mid) == {}


def test_upsert_vectors_and_delete_clear_hashes(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    h.write([_para("p0"), _para("p1")])
    h.embed()
    assert index.chunk_hashes(h.memory_id)
    index.upsert_vectors(h.memory_id, ["x"], [[1.0] * DIMS])
    assert index.chunk_hashes(h.memory_id) == {}
    assert index._vec_chunk_ids(h.memory_id) == [f"{h.memory_id}:0"]

    h.write([_para("p0"), _para("p1"), _para("p2")])
    h.embed()
    index.delete(h.memory_id)
    assert index._vec_chunk_ids(h.memory_id) == []
    assert index.chunk_hashes(h.memory_id) == {}


def test_reuse_plan_requires_matching_signature(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    h.write([_para("p0")])
    h.embed()
    hashes = list(index.chunk_hashes(h.memory_id).values())
    assert index.plan_vector_reuse(h.memory_id, hashes, h.signature).unchanged == {0}
    assert index.plan_vector_reuse(h.memory_id, hashes, "other") == VectorReusePlan()


def test_compaction_keeps_hashes_valid(vault: Vault, index: Index) -> None:
    h = _Harness(vault, index)
    paras = [_para(f"p{i}") for i in range(3)]
    h.write(paras)
    h.embed()
    index.compact_vectors(batch_pause_seconds=0)

    paras.append(_para("p3"))
    h.write(paras)
    assert h.embed() == [paras[3].strip()]
    h.assert_vectors_match(paras)
