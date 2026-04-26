"""Tests for `memstem.core.embed_worker.EmbedWorker`.

The pipeline pushes records onto `embed_queue`; the worker drains the
queue and writes vectors. Tests use a stub embedder so we don't talk
to a real provider — the worker logic is what matters here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.embed_worker import EmbedWorker, drain_once
from memstem.core.embeddings import Embedder, EmbeddingError
from memstem.core.index import Index, body_hash
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=8)
    idx.connect()
    yield idx
    idx.close()


def _record(body: str = "hello world", ref: str | None = None) -> MemoryRecord:
    return MemoryRecord(
        source="test",
        ref=ref or f"/tmp/{uuid4()}.md",
        title="t",
        body=body,
        tags=[],
        metadata={
            "type": "memory",
            "created": "2026-04-26T00:00:00+00:00",
            "updated": "2026-04-26T00:00:00+00:00",
        },
    )


class _StubEmbedder(Embedder):
    """Records calls; returns deterministic dummy vectors."""

    dimensions = 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_once = False
        self.fail_always = False

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_always:
            raise EmbeddingError("intentional always-fail")
        if self.fail_once:
            self.fail_once = False
            raise EmbeddingError("intentional one-shot fail")
        return [[float(i)] * 8 for i in range(len(texts))]


class TestTick:
    def test_drains_pending_records(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        for _ in range(3):
            pipe.process(_record())
        embedder = _StubEmbedder()
        worker = EmbedWorker(
            vault=vault, index=index, embedder=embedder, batch_size=10, idle_sleep=0
        )
        embedded = asyncio.run(worker.tick())
        assert embedded == 3
        # Queue is now empty.
        assert index.queue_stats() == {"pending": 0, "failed": 0, "total": 0}

    def test_writes_vec_rows(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="alpha"))
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=10,
            idle_sleep=0,
        )
        asyncio.run(worker.tick())
        rows = index.db.execute(
            "SELECT chunk_index FROM memories_vec WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchall()
        assert len(rows) == 1

    def test_failure_increments_retry_then_marks_failed(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        embedder = _StubEmbedder()
        embedder.fail_always = True
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=1,
            max_retries=2,
            idle_sleep=0,
        )

        # First tick: fails, retry_count=1, failed=0.
        asyncio.run(worker.tick())
        row = index.db.execute(
            "SELECT retry_count, failed, last_error FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["retry_count"] == 1
        assert row["failed"] == 0
        assert "always-fail" in row["last_error"]

        # Second tick: hits the cap, marked failed.
        asyncio.run(worker.tick())
        row = index.db.execute(
            "SELECT retry_count, failed FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["retry_count"] == 2
        assert row["failed"] == 1

        # Future ticks skip failed rows.
        embedded = asyncio.run(worker.tick())
        assert embedded == 0

    def test_recover_after_one_shot_failure(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        embedder = _StubEmbedder()
        embedder.fail_once = True
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=1,
            max_retries=5,
            idle_sleep=0,
        )

        asyncio.run(worker.tick())
        # Still in queue with retry_count=1.
        row = index.db.execute(
            "SELECT retry_count, failed FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["retry_count"] == 1
        assert row["failed"] == 0

        asyncio.run(worker.tick())
        # Now succeeds; queue cleared.
        assert (
            index.db.execute(
                "SELECT 1 FROM embed_queue WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            is None
        )

    def test_drops_queue_entry_when_vault_file_missing(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        # Delete the file out from under us.
        (vault.root / memory.path).unlink()

        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=1,
            idle_sleep=0,
        )
        asyncio.run(worker.tick())
        assert index.queue_stats() == {"pending": 0, "failed": 0, "total": 0}


class TestDrainOnce:
    def test_processes_all_then_returns(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        for _ in range(7):
            pipe.process(_record())
        result = asyncio.run(
            drain_once(
                vault=vault,
                index=index,
                embedder=_StubEmbedder(),
                batch_size=3,
                progress_every=1000,
            )
        )
        assert result["processed"] == 7
        assert result["failed_now"] == 0

    def test_drain_with_failed_records(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        embedder = _StubEmbedder()
        embedder.fail_always = True
        # Fail max_retries=1 → first tick marks it failed.
        result = asyncio.run(
            drain_once(vault=vault, index=index, embedder=embedder, batch_size=1, max_retries=1)
        )
        assert result["processed"] == 0
        assert result["failed_now"] == 1
        # Reset and retry with a working embedder → succeeds.
        index.reset_failed_queue()
        result = asyncio.run(
            drain_once(vault=vault, index=index, embedder=_StubEmbedder(), batch_size=1)
        )
        assert result["processed"] == 1
        assert (
            index.db.execute(
                "SELECT 1 FROM embed_queue WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            is None
        )


class TestQueueOps:
    def test_enqueue_idempotent_resets_state(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        index.mark_embed_error(str(memory.id), "boom", max_retries=1)
        assert (
            index.db.execute(
                "SELECT failed FROM embed_queue WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()["failed"]
            == 1
        )
        index.enqueue_embed(str(memory.id))
        row = index.db.execute(
            "SELECT failed, retry_count, last_error FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["failed"] == 0
        assert row["retry_count"] == 0
        assert row["last_error"] is None

    def test_reset_failed_queue(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        index.mark_embed_error(str(memory.id), "x", max_retries=1)
        assert index.queue_stats()["failed"] == 1
        n = index.reset_failed_queue()
        assert n == 1
        assert index.queue_stats() == {"pending": 1, "failed": 0, "total": 1}


class TestEmbedStateOnSuccess:
    """The worker stamps `embed_state` with body_hash + signature after
    a successful embed so the next pipeline pass can skip the record
    if neither has changed (PR #30)."""

    SIG = "stub:test:8"

    def test_records_state_with_signature(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="hello world"))
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=10,
            idle_sleep=0,
            embedding_signature=self.SIG,
        )
        asyncio.run(worker.tick())

        row = index.db.execute(
            "SELECT body_hash, embed_signature FROM embed_state WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row is not None
        assert row["body_hash"] == body_hash("hello world")
        assert row["embed_signature"] == self.SIG

    def test_state_not_recorded_on_failure(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record())
        embedder = _StubEmbedder()
        embedder.fail_always = True
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=1,
            max_retries=2,
            idle_sleep=0,
            embedding_signature=self.SIG,
        )
        asyncio.run(worker.tick())
        row = index.db.execute(
            "SELECT 1 FROM embed_state WHERE memory_id = ?", (str(memory.id),)
        ).fetchone()
        assert row is None

    def test_after_embed_pipeline_skips_unchanged_re_emit(self, vault: Vault, index: Index) -> None:
        """End-to-end: the next reconcile pass shouldn't re-enqueue
        a record whose body and signature haven't changed."""
        pipe = Pipeline(vault, index, embedding_signature=self.SIG)
        ref = "/tmp/stable.md"
        memory = pipe.process(_record(body="stable content", ref=ref))
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=10,
            idle_sleep=0,
            embedding_signature=self.SIG,
        )
        asyncio.run(worker.tick())
        # Queue empty after worker drain.
        assert index.queue_stats()["pending"] == 0

        # Reconcile re-emit with the same body — must NOT re-enqueue.
        pipe.process(_record(body="stable content", ref=ref))
        rows = index.db.execute(
            "SELECT 1 FROM embed_queue WHERE memory_id = ?", (str(memory.id),)
        ).fetchall()
        assert rows == []

    def test_empty_body_records_state(self, vault: Vault, index: Index) -> None:
        """Records with empty bodies still get stamped so the pipeline
        doesn't keep re-enqueueing them (chunk_text returns [])."""
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body=""))
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=1,
            idle_sleep=0,
            embedding_signature=self.SIG,
        )
        asyncio.run(worker.tick())
        row = index.db.execute(
            "SELECT body_hash, embed_signature FROM embed_state WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row is not None
        assert row["body_hash"] == body_hash("")
        assert row["embed_signature"] == self.SIG
