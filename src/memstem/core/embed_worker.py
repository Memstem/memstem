"""Async worker that drains the `embed_queue` and writes vectors.

The pipeline is fast-path only — it writes vault + memories + FTS5 and
enqueues the record. Actual embedding happens here, in a worker that
the daemon (or a one-shot `memstem embed`) runs continuously. Workers
batch chunks across records when the backend supports it, retry
transient failures with exponential backoff, and surrender after a
configurable number of attempts so a single bad record doesn't block
the queue.

Concurrency is task-level: ``run_workers(N, ...)`` spawns N async tasks
that share the queue. SQLite serializes writes, so the workers
naturally take different memory_ids without explicit locking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from memstem.core.embeddings import (
    Embedder,
    EmbeddingError,
    chunk_text,
)
from memstem.core.index import Index
from memstem.core.storage import MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)


class EmbedWorker:
    """Single async worker. Spawn N of these to scale concurrency.

    The worker loop:
    1. Pull up to ``batch_size`` pending memory_ids from the queue.
    2. For each, read the vault file, split into chunks.
    3. Send chunks to the embedder; write vectors on success.
    4. On embedder failure: log the error against the queue row and
       move on. The next pass will retry until ``max_retries``.
    5. Sleep ``idle_sleep`` seconds when the queue is empty.

    The worker is designed to be cancelled — call ``asyncio.CancelledError``
    bubbles up through the await points, and outstanding HTTP requests
    are abandoned cleanly.
    """

    def __init__(
        self,
        *,
        vault: Vault,
        index: Index,
        embedder: Embedder,
        batch_size: int = 8,
        max_retries: int = 5,
        idle_sleep: float = 5.0,
        backoff_base: float = 2.0,
        worker_id: int = 0,
    ) -> None:
        self.vault = vault
        self.index = index
        self.embedder = embedder
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.idle_sleep = idle_sleep
        self.backoff_base = backoff_base
        self.worker_id = worker_id

    async def run(self) -> None:
        """Run forever (until cancelled). Logs progress and errors."""
        logger.info("embed worker %d starting (batch_size=%d)", self.worker_id, self.batch_size)
        while True:
            try:
                processed = await self.tick()
            except asyncio.CancelledError:
                logger.info("embed worker %d cancelled", self.worker_id)
                raise
            except Exception as exc:  # pragma: no cover - paranoia
                logger.exception("embed worker %d crashed: %s", self.worker_id, exc)
                await asyncio.sleep(self.idle_sleep)
                continue
            if processed == 0:
                await asyncio.sleep(self.idle_sleep)

    async def tick(self) -> int:
        """One pass of the worker loop. Returns records actually embedded.

        Public for tests — call ``await worker.tick()`` to drain a batch
        without entering the long-running loop.
        """
        pending = self.index.queue_pending(limit=self.batch_size)
        if not pending:
            return 0

        embedded = 0
        for memory_id in pending:
            ok = await asyncio.to_thread(self._embed_one, memory_id)
            if ok:
                embedded += 1
        return embedded

    def _embed_one(self, memory_id: str) -> bool:
        """Embed one record. Sync — runs under :func:`asyncio.to_thread`
        so HTTP and SQLite I/O don't block the event loop."""
        try:
            chunks = self._chunks_for(memory_id)
        except _RecordMissingError:
            # Vault file gone — drop the queue entry; nothing to embed.
            self.index.dequeue_embed(memory_id)
            return False

        if not chunks:
            self.index.dequeue_embed(memory_id)
            return True

        try:
            vectors = self.embedder.embed_batch(chunks)
        except EmbeddingError as exc:
            logger.warning("embed worker %d: failed for %s: %s", self.worker_id, memory_id, exc)
            self.index.mark_embed_error(memory_id, str(exc), max_retries=self.max_retries)
            return False
        except Exception as exc:
            logger.warning(
                "embed worker %d: unexpected error for %s: %s",
                self.worker_id,
                memory_id,
                exc,
            )
            self.index.mark_embed_error(memory_id, repr(exc), max_retries=self.max_retries)
            return False

        try:
            self.index.upsert_vectors(memory_id, chunks, vectors)
        except ValueError as exc:
            logger.warning(
                "embed worker %d: vector upsert rejected for %s: %s",
                self.worker_id,
                memory_id,
                exc,
            )
            self.index.mark_embed_error(memory_id, str(exc), max_retries=self.max_retries)
            return False

        self.index.dequeue_embed(memory_id)
        return True

    def _chunks_for(self, memory_id: str) -> list[str]:
        row = self.index.db.execute(
            "SELECT path FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise _RecordMissingError(memory_id)
        try:
            memory = self.vault.read(row["path"])
        except MemoryNotFoundError as exc:
            raise _RecordMissingError(memory_id) from exc
        return chunk_text(memory.body)


class _RecordMissingError(Exception):
    """Internal marker: vault file or memory row went away mid-embed."""


async def run_workers(
    n: int,
    *,
    vault: Vault,
    index: Index,
    embedder: Embedder,
    batch_size: int = 8,
    max_retries: int = 5,
    idle_sleep: float = 5.0,
) -> None:
    """Spawn `n` workers and gather them. Cancellation propagates."""
    if n < 1:
        raise ValueError("must spawn at least one worker")
    workers = [
        EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=batch_size,
            max_retries=max_retries,
            idle_sleep=idle_sleep,
            worker_id=i,
        )
        for i in range(n)
    ]
    tasks = [asyncio.create_task(w.run()) for w in workers]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        # Best-effort drain; any cancellation errors are expected.
        await asyncio.gather(*tasks, return_exceptions=True)


async def drain_once(
    *,
    vault: Vault,
    index: Index,
    embedder: Embedder,
    batch_size: int = 8,
    max_retries: int = 5,
    progress_every: int = 25,
    on_progress: Any = None,
) -> dict[str, int]:
    """One-shot drain: run a single worker until the queue is empty.

    Used by ``memstem embed`` (manual drain) and tests. Returns
    ``{processed, failed_now}`` summary.
    """
    worker = EmbedWorker(
        vault=vault,
        index=index,
        embedder=embedder,
        batch_size=batch_size,
        max_retries=max_retries,
        idle_sleep=0.0,
    )
    total = 0
    while True:
        n = await worker.tick()
        if n == 0:
            break
        total += n
        if on_progress and progress_every and total % progress_every < n:
            on_progress(total)
    stats = index.queue_stats()
    return {"processed": total, "failed_now": stats["failed"]}


__all__ = ["EmbedWorker", "drain_once", "run_workers"]
