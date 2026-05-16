"""Tests for `memstem.core.embed_worker.EmbedWorker`.

The pipeline pushes records onto `embed_queue`; the worker drains the
queue and writes vectors. Tests use a stub embedder so we don't talk
to a real provider — the worker logic is what matters here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.embed_worker import EmbedWorker, drain_once
from memstem.core.embeddings import Embedder, EmbeddingError, TransientEmbeddingError
from memstem.core.index import Index, body_hash
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault


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


def _processed(pipe: Pipeline, record: MemoryRecord) -> Memory:
    """Pipeline.process wrapper that asserts the record wasn't noise-filtered."""
    memory = pipe.process(record)
    assert memory is not None, "pipeline unexpectedly noise-filtered the test record"
    return memory


class _StubEmbedder(Embedder):
    """Records calls; returns deterministic dummy vectors.

    Toggle ``fail_once`` / ``fail_always`` for permanent
    :class:`EmbeddingError`. Toggle ``transient_once`` /
    ``transient_always`` for :class:`TransientEmbeddingError` (the
    network-blip / 5xx shape that should NOT count against
    ``retry_count``).
    """

    dimensions = 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_once = False
        self.fail_always = False
        self.transient_once = False
        self.transient_always = False

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.transient_always:
            raise TransientEmbeddingError("intentional transient always")
        if self.transient_once:
            self.transient_once = False
            raise TransientEmbeddingError("intentional transient one-shot")
        if self.fail_always:
            raise EmbeddingError("intentional always-fail")
        if self.fail_once:
            self.fail_once = False
            raise EmbeddingError("intentional one-shot fail")
        return [[float(i)] * 8 for i in range(len(texts))]


class TestTick:
    def test_drains_pending_records(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        for i in range(3):
            _processed(pipe, _record(body=f"distinct body {i}"))
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
        memory = _processed(pipe, _record(body="alpha"))
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record())
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


class TestTransientHandling:
    """:class:`TransientEmbeddingError` (network blip, 5xx, read
    timeout) must NOT bump the record's ``retry_count`` and must drive
    the worker's exponential backoff streak. Otherwise a 30-second
    OpenAI hiccup permanently fails every record in flight (the
    behaviour observed on Ari's box pre-fix).
    """

    def test_transient_does_not_bump_retry_count(self, vault: Vault, index: Index) -> None:
        """The classic Ari shape: OpenAI returns peer-closed-connection
        twice in a row, then succeeds. Permanent error handling would
        burn through retries; transient handling shouldn't touch
        ``retry_count`` at all and the eventual success cleans the
        queue."""
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record())
        embedder = _StubEmbedder()
        worker = EmbedWorker(
            vault=vault, index=index, embedder=embedder, batch_size=1, idle_sleep=0
        )

        # First tick: transient. retry_count must stay at 0 because
        # we don't punish the queue for transport failures.
        embedder.transient_once = True
        asyncio.run(worker.tick())
        row = index.db.execute(
            "SELECT retry_count, failed, last_error FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["retry_count"] == 0, "transient error must not bump retry_count"
        assert row["failed"] == 0
        assert row["last_error"] is None, (
            "transient error must not write last_error — that's reserved for "
            "permanent failures the operator might want to inspect"
        )

        # Second tick: succeeds; queue is cleaned.
        asyncio.run(worker.tick())
        assert (
            index.db.execute(
                "SELECT 1 FROM embed_queue WHERE memory_id = ?",
                (str(memory.id),),
            ).fetchone()
            is None
        )

    def test_transient_streak_drives_backoff(self, vault: Vault, index: Index) -> None:
        """Consecutive transient ticks bump the worker's backoff
        counter, so the next sleep gets longer. A successful tick
        resets the streak."""
        pipe = Pipeline(vault, index)
        _processed(pipe, _record())
        embedder = _StubEmbedder()
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=1,
            idle_sleep=1.0,
            backoff_base=2.0,
        )

        assert worker._transient_streak == 0
        assert worker._transient_sleep() == 1.0  # base idle when no streak

        # First transient tick: streak=1, sleep stays at base (idle * base^0).
        embedder.transient_always = True
        asyncio.run(worker.tick())
        assert worker._transient_streak == 1
        assert worker._transient_sleep() == pytest.approx(1.0)

        # Second transient tick: streak=2, sleep = base * base^1 = 2.0.
        asyncio.run(worker.tick())
        assert worker._transient_streak == 2
        assert worker._transient_sleep() == pytest.approx(2.0)

        # Third: streak=3, sleep = base * base^2 = 4.0.
        asyncio.run(worker.tick())
        assert worker._transient_streak == 3
        assert worker._transient_sleep() == pytest.approx(4.0)

        # Backoff caps at MAX_TRANSIENT_BACKOFF (so a multi-hour
        # provider outage doesn't translate to multi-hour sleeps).
        worker._transient_streak = 50
        assert worker._transient_sleep() == EmbedWorker.MAX_TRANSIENT_BACKOFF

        # First success resets the streak.
        embedder.transient_always = False
        asyncio.run(worker.tick())
        assert worker._transient_streak == 0
        assert worker._transient_sleep() == 1.0

    def test_permanent_error_does_not_touch_streak(self, vault: Vault, index: Index) -> None:
        """A genuine permanent failure (4xx, schema rejection) goes
        through ``mark_embed_error`` and bumps retry_count, but it must
        NOT drive the transient backoff — that's reserved for
        infrastructure flakiness, not bad records."""
        pipe = Pipeline(vault, index)
        _processed(pipe, _record())
        embedder = _StubEmbedder()
        embedder.fail_always = True
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=1,
            max_retries=10,
            idle_sleep=1.0,
        )

        asyncio.run(worker.tick())
        # Permanent error path bumped retry_count.
        row = index.db.execute(
            "SELECT retry_count FROM embed_queue WHERE memory_id IS NOT NULL"
        ).fetchone()
        assert row["retry_count"] == 1
        # Transient streak untouched.
        assert worker._transient_streak == 0


class TestDrainOnce:
    def test_processes_all_then_returns(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        for i in range(7):
            _processed(pipe, _record(body=f"distinct body {i}"))
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record(body="hello world"))
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
        memory = _processed(pipe, _record())
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
        memory = _processed(pipe, _record(body="stable content", ref=ref))
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
        _processed(pipe, _record(body="stable content", ref=ref))
        rows = index.db.execute(
            "SELECT 1 FROM embed_queue WHERE memory_id = ?", (str(memory.id),)
        ).fetchall()
        assert rows == []

    def test_empty_body_records_state(self, vault: Vault, index: Index) -> None:
        """Records with empty bodies still get stamped so the pipeline
        doesn't keep re-enqueueing them (chunk_text returns [])."""
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body=""))
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

    def test_worker_advances_when_parent_deleted_mid_embed(
        self, vault: Vault, index: Index
    ) -> None:
        """Regression for the FK race that caused
        ``sqlite3.IntegrityError: FOREIGN KEY constraint failed`` and
        crashed the worker on Ari's box during the v0.7 → v0.8
        embedder migration: the parent ``memories`` row gets deleted
        while the worker is round-tripping the embedder, then the
        worker comes back and tries to ``record_embed_state`` for an
        id whose cascade has already nuked its queue/state rows.

        We simulate the race deterministically by having the embedder
        delete the parent inside ``embed_batch`` — the call sequence
        is then:
          1. ``_read_for_embed``: succeeds (vault file still there)
          2. ``embedder.embed_batch``: deletes parent as a side effect,
             returns vectors
          3. ``upsert_vectors``: succeeds (vec0 doesn't check FK)
          4. ``record_embed_state``: pre-fix → IntegrityError →
             worker.tick() crashes; post-fix → silent no-op.
        """

        class _ParentDeletingEmbedder(_StubEmbedder):
            def __init__(self, idx: Index, mid: str) -> None:
                super().__init__()
                self._idx = idx
                self._mid = mid

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                # Race: delete the parent memory mid-flight, mirroring
                # what ``Index.upsert``'s path-displacement path does
                # when a different memory_id claims the same path.
                self._idx.delete(self._mid)
                return super().embed_batch(texts)

        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="will-disappear"))
        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_ParentDeletingEmbedder(index, str(memory.id)),
            batch_size=1,
            idle_sleep=0,
            embedding_signature=self.SIG,
        )

        # Pre-fix this raises sqlite3.IntegrityError out of `tick()`.
        # Post-fix it returns cleanly.
        asyncio.run(worker.tick())

        # No state row (parent is gone), no orphan vec rows
        # (record_embed_state cleans them in the FK-fail branch).
        assert (
            index.db.execute(
                "SELECT 1 FROM embed_state WHERE memory_id = ?", (str(memory.id),)
            ).fetchone()
            is None
        )
        assert (
            index.db.execute(
                "SELECT 1 FROM memories_vec WHERE memory_id = ?", (str(memory.id),)
            ).fetchone()
            is None
        )


class TestConcurrencyRace:
    """Regression: under contention the embed worker used to crash with
    ``sqlite3.InterfaceError: bad parameter or other API misuse`` when
    other threads in the same process touched the shared
    :class:`sqlite3.Connection` without taking ``Index._lock``.

    The pipeline's ID-lookup paths (called on the asyncio thread by the
    watch loop) ran ``index.db.execute(...)`` directly. The embed worker
    (running in ``asyncio.to_thread``) took the lock for its calls. Two
    threads → same connection → unequal serialization → SQLite returns
    SQLITE_MISUSE intermittently.

    These tests run the contended access patterns side-by-side and
    assert no thread sees an unexpected error. They reproduce the
    crash deterministically against the pre-fix code.
    """

    def _seed(self, vault: Vault, index: Index, n: int) -> list[str]:
        pipe = Pipeline(vault, index)
        return [
            str(_processed(pipe, _record(body=f"row {i}", ref=f"ref-{i}")).id) for i in range(n)
        ]

    @staticmethod
    def _run_under_contention(
        target: Callable[[], None],
        contender: Callable[[], None],
        duration: float = 1.5,
    ) -> tuple[list[BaseException], list[BaseException]]:
        target_errors: list[BaseException] = []
        contender_errors: list[BaseException] = []
        stop = threading.Event()

        def loop(fn: Callable[[], None], sink: list[BaseException]) -> None:
            while not stop.is_set():
                try:
                    fn()
                except BaseException as exc:
                    sink.append(exc)
                    return

        threads = [
            threading.Thread(target=loop, args=(target, target_errors), daemon=True),
            threading.Thread(target=loop, args=(contender, contender_errors), daemon=True),
            threading.Thread(target=loop, args=(contender, contender_errors), daemon=True),
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and not target_errors and not contender_errors:
            time.sleep(0.02)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        return target_errors, contender_errors

    def test_pipeline_lookups_serialize_with_embed_worker(self, vault: Vault, index: Index) -> None:
        """Pipeline ``_lookup_id_or_none`` / ``_existing_path`` MUST go
        through the Index lock. Otherwise they race the embed worker's
        ``get_path`` and SQLite returns SQLITE_MISUSE."""
        ids = self._seed(vault, index, 50)
        pipe = Pipeline(vault, index)

        def worker_lookup() -> None:
            for mid in ids:
                index.get_path(mid)

        def pipeline_lookup() -> None:
            for i in range(len(ids)):
                pipe._lookup_id_or_none("test", f"ref-{i}")
                pipe._existing_path(uuid4())

        target_errors, contender_errors = self._run_under_contention(worker_lookup, pipeline_lookup)
        assert not target_errors, f"Index.get_path raised under contention: {target_errors[0]!r}"
        assert not contender_errors, (
            f"Pipeline lookup raised under contention: {contender_errors[0]!r}"
        )

    def test_index_lookup_record_mapping_is_serialized(self, vault: Vault, index: Index) -> None:
        """The public Index lookup that pipeline now uses for
        ``(source, ref) → memory_id`` must also be safe to call from
        any thread without external locking."""
        self._seed(vault, index, 25)

        def worker_lookup() -> None:
            index.queue_pending(limit=8)

        def contender_lookup() -> None:
            for i in range(25):
                index.lookup_record_mapping("test", f"ref-{i}")

        target_errors, contender_errors = self._run_under_contention(
            worker_lookup, contender_lookup
        )
        assert not target_errors and not contender_errors, (
            f"contention raised: target={target_errors!r} contender={contender_errors!r}"
        )

    def test_worker_survives_pipeline_writes_under_transient_failure(
        self, vault: Vault, index: Index
    ) -> None:
        """End-to-end shape from the field report: while OpenAI returns
        503/timeouts the worker spins tightly through the queue while
        the pipeline's watcher keeps processing fresh records. Without
        the fix the worker's threadpool job crashes with
        InterfaceError; with the fix it just keeps spinning.
        """
        ids = self._seed(vault, index, 30)
        embedder = _StubEmbedder()
        embedder.transient_always = True

        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=embedder,
            batch_size=4,
            max_retries=10,
            idle_sleep=0,
        )

        pipe = Pipeline(vault, index)

        async def spin_worker() -> None:
            deadline = asyncio.get_event_loop().time() + 1.0
            while asyncio.get_event_loop().time() < deadline:
                await worker.tick()

        def pipeline_thread() -> None:
            for i in range(len(ids)):
                pipe._lookup_id_or_none("test", f"ref-{i}")
                pipe._existing_path(uuid4())

        contender = threading.Thread(target=pipeline_thread, daemon=True)
        contender.start()
        try:
            asyncio.run(spin_worker())
        finally:
            contender.join(timeout=2.0)
        # If we got here without a sqlite3 exception bubbling out of
        # the worker, the lock serialization holds. Worker loop logs
        # transient embedder errors but does not crash.


class TestReadForEmbedRecovery:
    """A vault file with broken frontmatter must not crash the embed
    worker. The pre-fix code only caught :class:`MemoryNotFoundError`,
    so a malformed-but-existing record bubbled
    :class:`memstem.core.storage.InvalidFrontmatterError` up to ``run()``
    and the worker would log an "embed worker N crashed" stack on every
    tick forever.
    """

    def test_invalid_frontmatter_marks_failed_instead_of_crashing(
        self, vault: Vault, index: Index
    ) -> None:
        pipe = Pipeline(vault, index)
        memory = _processed(pipe, _record(body="alpha"))
        # Stomp the file with an empty (but parseable) frontmatter block.
        (vault.root / memory.path).write_text("---\n---\n\nbody\n", encoding="utf-8")

        worker = EmbedWorker(
            vault=vault,
            index=index,
            embedder=_StubEmbedder(),
            batch_size=1,
            idle_sleep=0,
        )
        # Must not raise. Must mark the queue entry failed so the
        # worker advances rather than hot-looping the same bad record,
        # but the failed state stays visible in `memstem doctor` so
        # the operator knows a vault file needs attention.
        embedded = asyncio.run(worker.tick())
        assert embedded == 0
        row = index.db.execute(
            "SELECT failed, last_error FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["failed"] == 1
        assert "invalid frontmatter" in row["last_error"]

        # Subsequent ticks skip the failed row instead of crashing.
        assert asyncio.run(worker.tick()) == 0
