"""Async worker that drains the `embed_queue` and writes vectors.

The pipeline is fast-path only — it writes vault + memories + FTS5 and
enqueues the record. Actual embedding happens here, in a worker that
the daemon (or a one-shot `memstem embed`) runs continuously. Workers
batch chunks across records when the backend supports it, retry
transient failures with exponential backoff, and surrender after a
configurable number of attempts so a single bad record doesn't block
the queue.

Concurrency is task-level: ``run_workers(N, ...)`` spawns N async tasks
that share the queue. Each worker claims its rows atomically via
``Index.claim_pending`` (a leased claim, recoverable if the worker
crashes), so concurrent workers — including a parallel ``memstem
embed`` process — never embed the same record twice in flight.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from memstem.core.embeddings import (
    Embedder,
    EmbeddingError,
    TransientEmbeddingError,
    chunk_text,
)
from memstem.core.index import Index, body_hash
from memstem.core.media import extract_image_refs, image_file_to_data_url
from memstem.core.storage import InvalidFrontmatterError, MemoryNotFoundError, Vault

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

    # Cap on the per-tick transient-error backoff. Without a cap, a long
    # provider outage would push the sleep into hours; this keeps the
    # worker checking back at least once a minute so a recovering
    # provider gets picked up promptly.
    MAX_TRANSIENT_BACKOFF: float = 60.0

    # How long a queue-row claim stays valid. Generous relative to a
    # batch (8 records x a few seconds each); only a worker that crashed
    # or hung mid-batch ever lets a claim age out, at which point the
    # row becomes reclaimable by any worker. Normal completion releases
    # the claim within seconds via dequeue / mark_embed_error / release.
    CLAIM_LEASE_SECONDS: float = 300.0

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
        embedding_signature: str = "",
    ) -> None:
        self.vault = vault
        self.index = index
        self.embedder = embedder
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.idle_sleep = idle_sleep
        self.backoff_base = backoff_base
        self.worker_id = worker_id
        self.embedding_signature = embedding_signature
        # Queue-claim identity: distinguishes this worker from siblings in
        # the same daemon AND from a parallel `memstem embed` process.
        self._claimant = f"pid{os.getpid()}:w{worker_id}"
        # Consecutive transient-error count, used for exponential backoff
        # between ticks. Reset to 0 on any successful tick.
        self._transient_streak = 0

    def _transient_sleep(self) -> float:
        """Seconds to sleep after a tick that hit a transient error.

        Exponential backoff in ``backoff_base``: idle_sleep,
        idle_sleep*base, idle_sleep*base^2, …, capped at
        :attr:`MAX_TRANSIENT_BACKOFF`. Resets to ``idle_sleep`` once the
        worker sees a successful tick (``_transient_streak == 0``).
        """
        if self._transient_streak <= 0:
            return self.idle_sleep
        delay = self.idle_sleep * (self.backoff_base ** (self._transient_streak - 1))
        return min(delay, self.MAX_TRANSIENT_BACKOFF)

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
                # Either the queue was empty (no streak, plain idle) or
                # the only attempts failed transiently (streak active,
                # back off exponentially so a 30-second OpenAI hiccup
                # doesn't translate to N-workers-times-M-records of
                # wasted round trips per second).
                await asyncio.sleep(self._transient_sleep())

    async def tick(self) -> int:
        """One pass of the worker loop. Returns records actually embedded.

        Public for tests — call ``await worker.tick()`` to drain a batch
        without entering the long-running loop.

        Tracks a per-worker consecutive-transient streak via
        ``_embed_one``'s return value:
        - ``True`` (success) → reset the streak; the run loop sleeps
          its normal ``idle_sleep`` next cycle.
        - ``False`` after a transient embedder error → bump the
          streak; the run loop sleeps with exponential backoff.
        - ``False`` after a permanent error → leave the streak alone
          (don't punish the queue for a single bad record).
        """
        claimed = self.index.claim_pending(
            limit=self.batch_size,
            claimant=self._claimant,
            lease_seconds=self.CLAIM_LEASE_SECONDS,
        )
        if not claimed:
            return 0

        embedded = 0
        any_transient = False
        for memory_id, _enqueued_at in claimed:
            ok, transient = await asyncio.to_thread(self._embed_one, memory_id)
            if ok:
                embedded += 1
            elif transient:
                any_transient = True

        if embedded > 0:
            # Any successful embed clears the backoff. Even if some
            # records in the same batch hit transients, we know the
            # provider is at least partially healthy.
            self._transient_streak = 0
        elif any_transient:
            self._transient_streak += 1
        return embedded

    def _embed_one(self, memory_id: str) -> tuple[bool, bool]:
        """Embed one record. Sync — runs under :func:`asyncio.to_thread`
        so HTTP and SQLite I/O don't block the event loop.

        Returns ``(ok, transient)``:
        - ``ok`` is ``True`` iff the record was successfully embedded.
        - ``transient`` is ``True`` iff the failure was transient
          (network blip, 5xx) and the caller should back off rather
          than count this against ``retry_count``.
        """
        try:
            body, text_chunks, image_urls = self._read_for_embed(memory_id)
        except _RecordMissingError:
            # Vault file gone — drop the queue entry; nothing to embed.
            self.index.dequeue_embed(memory_id)
            return False, False
        except InvalidFrontmatterError as exc:
            # Vault file exists but its frontmatter no longer validates
            # (manual edit, half-written file, schema migration not yet
            # applied). Re-reading on every tick would just keep
            # crashing — surface it through the normal failed-record
            # path so it stops the queue churn and shows up in
            # `memstem doctor`. The next `memstem reindex` (or a fix
            # to the file) re-enqueues with a clean retry_count.
            logger.warning(
                "embed worker %d: invalid frontmatter for %s: %s "
                "(marking failed; fix the vault file then reset)",
                self.worker_id,
                memory_id,
                exc,
            )
            self.index.mark_embed_error(memory_id, f"invalid frontmatter: {exc}", max_retries=1)
            return False, False

        if not text_chunks and not image_urls:
            # Empty body still counts as a successful "embed" — record
            # the state so the pipeline doesn't keep re-enqueueing it.
            self.index.record_embed_state(memory_id, body_hash(body), self.embedding_signature)
            self.index.dequeue_embed(memory_id)
            return True, False

        try:
            # Text chunks first, then any image media-chunks (ADR 0025); both
            # land in the same vector space, ordered text-then-image so
            # chunk_index 0 stays the first text chunk (MMR reads it).
            vectors = self.embedder.embed_batch(text_chunks) if text_chunks else []
            if image_urls:
                vectors = vectors + self.embedder.embed_images(image_urls)
        except TransientEmbeddingError as exc:
            # Network blip / 5xx / read timeout. The next tick can try
            # the same record again without burning a retry slot — a
            # 30-second OpenAI hiccup shouldn't permanently fail every
            # in-flight record.
            logger.warning(
                "embed worker %d: transient failure for %s: %s (will retry, retry_count unchanged)",
                self.worker_id,
                memory_id,
                exc,
            )
            # Hand the row straight back instead of letting the claim age
            # out — a healthier worker (or this one, next tick) can retry
            # immediately.
            self.index.release_embed_claim(memory_id)
            return False, True
        except EmbeddingError as exc:
            logger.warning("embed worker %d: failed for %s: %s", self.worker_id, memory_id, exc)
            self.index.mark_embed_error(memory_id, str(exc), max_retries=self.max_retries)
            return False, False
        except Exception as exc:
            logger.warning(
                "embed worker %d: unexpected error for %s: %s",
                self.worker_id,
                memory_id,
                exc,
            )
            self.index.mark_embed_error(memory_id, repr(exc), max_retries=self.max_retries)
            return False, False

        try:
            # upsert_vectors only length-matches chunks↔vectors (the vec
            # table stores no chunk text), so image media-chunks get
            # lightweight labels; their embedding is what matters.
            all_chunks = text_chunks + [f"<image:{i}>" for i in range(len(image_urls))]
            self.index.upsert_vectors(memory_id, all_chunks, vectors)
        except ValueError as exc:
            logger.warning(
                "embed worker %d: vector upsert rejected for %s: %s",
                self.worker_id,
                memory_id,
                exc,
            )
            self.index.mark_embed_error(memory_id, str(exc), max_retries=self.max_retries)
            return False, False

        self.index.record_embed_state(memory_id, body_hash(body), self.embedding_signature)
        self.index.dequeue_embed(memory_id)
        return True, False

    def _read_for_embed(self, memory_id: str) -> tuple[str, list[str], list[str]]:
        """Read the memory body, split into text chunks, and gather data-URLs
        for any local images it references (ADR 0025 media-chunks).

        Returns ``(body, text_chunks, image_data_urls)``. The body is
        returned so the caller can hash it for ``embed_state``. Image
        gathering is skipped entirely for text-only embedders.
        """
        rel_path = self.index.get_path(memory_id)
        if rel_path is None:
            raise _RecordMissingError(memory_id)
        try:
            memory = self.vault.read(rel_path)
        except MemoryNotFoundError as exc:
            raise _RecordMissingError(memory_id) from exc
        text_chunks = chunk_text(memory.body)
        image_urls = self._gather_image_urls(rel_path, memory.body)
        return memory.body, text_chunks, image_urls

    def _gather_image_urls(self, rel_path: str, body: str) -> list[str]:
        """Data-URLs for local, in-vault images referenced by ``body``.

        Only runs for multimodal embedders (``supports_images``). An image
        must resolve to an existing file inside the vault root; remote,
        missing, or vault-escaping paths are skipped. A single unreadable
        image is logged and skipped rather than failing the whole record.
        """
        if not self.embedder.supports_images:
            return []
        base_dir = (self.vault.root / rel_path).parent
        urls: list[str] = []
        for ref in extract_image_refs(body, base_dir):
            try:
                if not ref.path.is_file() or not ref.path.is_relative_to(self.vault.root):
                    continue
                urls.append(image_file_to_data_url(ref.path))
            except OSError as exc:
                logger.warning(
                    "embed worker %d: skipping unreadable image %s: %s",
                    self.worker_id,
                    ref.path,
                    exc,
                )
        return urls


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
    embedding_signature: str = "",
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
            embedding_signature=embedding_signature,
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
    embedding_signature: str = "",
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
        embedding_signature=embedding_signature,
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
