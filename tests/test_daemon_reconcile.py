"""Tests for the daemon's non-blocking startup reconcile.

The startup catch-up (`_reconcile_all`) runs as a background task so the
HTTP/MCP server, watchers, and embed workers come up immediately. These
tests pin the behaviours that make that safe: it processes every stream,
a failure in one stream never propagates (the daemon's live watchers
must stay up regardless), and the CPU-bound walk cedes the event loop so
the server isn't starved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.cli import (
    _periodic_reconcile,
    _reconcile_all,
    _reconcile_into_pipeline,
    _reconcile_skip_unchanged,
)
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault


def _record(ref: str, body: str) -> MemoryRecord:
    return MemoryRecord(
        source="openclaw",
        ref=ref,
        title="t",
        body=body,
        tags=[],
        metadata={
            "type": "memory",
            "created": "2026-04-25T10:00:00+00:00",
            "updated": "2026-04-25T10:00:00+00:00",
        },
    )


async def _stream(records: list[MemoryRecord]) -> AsyncGenerator[MemoryRecord, None]:
    for record in records:
        yield record


async def _failing_stream(record: MemoryRecord) -> AsyncGenerator[MemoryRecord, None]:
    yield record
    raise RuntimeError("adapter blew up mid-reconcile")


def _memory_count(index: Index) -> int:
    row = index.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    return int(row["c"])


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


async def test_reconcile_all_processes_every_stream(vault: Vault, index: Index) -> None:
    pipeline = Pipeline(vault, index)
    await _reconcile_all(
        pipeline,
        [
            (
                _stream([_record("/a/1.md", "alpha one"), _record("/a/2.md", "alpha two")]),
                "openclaw",
            ),
            (_stream([_record("/b/1.md", "bravo one")]), "claude-code"),
        ],
    )
    assert _memory_count(index) == 3
    assert index.lookup_record_mapping("openclaw", "/b/1.md") is not None


async def test_reconcile_all_swallows_stream_failure(vault: Vault, index: Index) -> None:
    pipeline = Pipeline(vault, index)
    # Must NOT raise even though the second stream blows up — a reconcile
    # failure cannot be allowed to take the daemon down.
    await _reconcile_all(
        pipeline,
        [
            (_stream([_record("/a/1.md", "alpha one")]), "openclaw"),
            (_failing_stream(_record("/b/1.md", "bravo one")), "claude-code"),
        ],
    )
    # The healthy stream's record still landed before the failure.
    assert index.lookup_record_mapping("openclaw", "/a/1.md") is not None


async def test_reconcile_cedes_control_to_event_loop(vault: Vault, index: Index) -> None:
    """A concurrent task must get a turn before the whole stream is done.

    Regression guard for the 0.12.2 fix: without periodic `asyncio.sleep(0)`
    the synchronous catch-up walk monopolizes the single-threaded event
    loop, so the canary's lone `sleep(0)` would only resolve after all 150
    records were processed (it would read 150). Cooperative yielding lets
    it regain control partway through.
    """
    pipeline = Pipeline(vault, index)
    records = [_record(f"/c/{i}.md", f"body number {i}") for i in range(150)]
    processed_when_canary_ran: list[int] = []

    async def canary() -> None:
        await asyncio.sleep(0)  # hand control to the reconcile task first
        processed_when_canary_ran.append(_memory_count(index))

    await asyncio.gather(
        _reconcile_all(pipeline, [(_stream(records), "openclaw")]),
        canary(),
    )

    assert processed_when_canary_ran[0] < 150
    assert _memory_count(index) == 150


async def test_reconcile_keeps_loop_responsive_when_process_is_slow(
    vault: Vault, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow per-record upsert must not starve the event loop (issue #142).

    ``Pipeline.process`` does synchronous markdown writes + index upserts.
    Running it inline on the loop pinned the loop thread at 100% CPU and held
    ``Index._lock`` back-to-back, so the HTTP/MCP ``/search`` handler saw
    ~15-20s latency mid-reconcile. The prior mitigation (a per-record
    ``await asyncio.sleep(0)``) was NOT enough: it cedes one tick, then the
    next synchronous ``process`` immediately reblocks the loop. The fix runs
    ``process`` in a worker thread (``asyncio.to_thread``), keeping the loop
    free for other coroutines.

    Here each ``process`` blocks its thread for 50ms; a concurrent 5ms
    heartbeat must keep ticking throughout. With the work on the loop the
    heartbeat is starved (a handful of ticks); off the loop it advances freely.
    """
    import time

    pipeline = Pipeline(vault, index)
    records = [_record(f"/slow/{i}.md", f"body number {i}") for i in range(10)]

    def _slow_process(_rec: MemoryRecord) -> None:
        time.sleep(0.05)  # simulate a heavy upsert; blocks the calling thread

    monkeypatch.setattr(pipeline, "process", _slow_process)

    done = asyncio.Event()
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    hb = asyncio.create_task(heartbeat())
    try:
        await _reconcile_into_pipeline(pipeline, _stream(records), "openclaw")
    finally:
        done.set()
        await hb

    # ~0.5s of blocking work ran off the loop, so the 5ms heartbeat had room to
    # advance many times. Inline (the bug) it would be in the single digits.
    assert ticks >= 20


def test_skip_unchanged_true_for_identical_record(vault: Vault, index: Index) -> None:
    """An already-stored, identical record is skipped (ADR 0024).

    The skip keys on ``body_hash_index``, which ``Pipeline.process``
    records directly — so a record is skippable straight after ingest,
    with no dependency on the embed worker having run.
    """
    pipeline = Pipeline(vault, index)
    pipeline.process(_record("/s/1.md", "the body never changed"))
    assert _reconcile_skip_unchanged(pipeline, _record("/s/1.md", "the body never changed")) is True


def test_skip_unchanged_true_even_when_not_embedded(vault: Vault, index: Index) -> None:
    """Stored but not yet embedded is still skipped — the signal is
    ``body_hash_index`` (written by process), not ``embed_state`` (written
    by the embed worker). This is what lets the skip converge while the
    embedder is degraded.
    """
    pipeline = Pipeline(vault, index)
    pipeline.process(_record("/s/3.md", "stored not embedded"))  # no embed worker run
    assert _reconcile_skip_unchanged(pipeline, _record("/s/3.md", "stored not embedded")) is True


def test_skip_unchanged_false_for_new_record(vault: Vault, index: Index) -> None:
    """A never-stored record must be processed, not skipped."""
    pipeline = Pipeline(vault, index)
    assert _reconcile_skip_unchanged(pipeline, _record("/s/new.md", "brand new")) is False


def test_skip_unchanged_false_for_changed_body(vault: Vault, index: Index) -> None:
    """Same ref, different body → not unchanged → must be processed."""
    pipeline = Pipeline(vault, index)
    pipeline.process(_record("/s/2.md", "original body"))
    assert _reconcile_skip_unchanged(pipeline, _record("/s/2.md", "edited body")) is False


async def test_reconcile_skips_unchanged_records_on_second_pass(vault: Vault, index: Index) -> None:
    """A second reconcile over identical content recognizes every record as
    unchanged — converges after one pass, no embedder required."""
    pipeline = Pipeline(vault, index)
    records = [_record(f"/s/{i}.md", f"body {i}") for i in range(5)]
    await _reconcile_all(pipeline, [(_stream(records), "openclaw")])
    assert _memory_count(index) == 5
    # The first pass recorded each body hash; the second pass skips all.
    assert all(_reconcile_skip_unchanged(pipeline, rec) for rec in records)
    await _reconcile_all(pipeline, [(_stream(list(records)), "openclaw")])
    assert _memory_count(index) == 5


# ─── periodic reconcile (B4 self-heal) ────────────────────────────


async def test_periodic_reconcile_repeats_with_fresh_streams(vault: Vault, index: Index) -> None:
    """Each cycle ingests via a FRESH set of one-shot generators, so a
    record that appeared after a watcher died is still picked up."""
    pipeline = Pipeline(vault, index)
    cycles = 0

    def make_streams() -> list[tuple[AsyncGenerator[MemoryRecord, None], str]]:
        nonlocal cycles
        cycles += 1
        return [(_stream([_record(f"/p/{cycles}.md", f"body {cycles}")]), "openclaw")]

    task = asyncio.create_task(_periodic_reconcile(pipeline, make_streams, 0.01))
    try:
        # Wait on the committed count, not the `cycles` counter: `cycles`
        # increments when a stream is BUILT, but processing now yields the loop
        # (records upsert in a worker thread, issue #142), so a record may not
        # be committed yet when its cycle's stream is built. Two committed
        # records require two cycles with fresh streams — exactly the intent.
        async with asyncio.timeout(5):
            while _memory_count(index) < 2:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert _memory_count(index) >= 2
    assert cycles >= 2


async def test_periodic_reconcile_survives_a_failed_cycle(vault: Vault, index: Index) -> None:
    """A failed sweep is logged and retried next interval, never fatal."""
    pipeline = Pipeline(vault, index)
    attempts = 0

    def make_streams() -> list[tuple[AsyncGenerator[MemoryRecord, None], str]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("adapter walk blew up")
        return [(_stream([_record("/p/late.md", "late body")]), "openclaw")]

    task = asyncio.create_task(_periodic_reconcile(pipeline, make_streams, 0.01))
    try:
        async with asyncio.timeout(5):
            while _memory_count(index) < 1:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert attempts >= 2


async def test_periodic_reconcile_disabled_returns_immediately(vault: Vault, index: Index) -> None:
    pipeline = Pipeline(vault, index)

    def make_streams() -> list[tuple[AsyncGenerator[MemoryRecord, None], str]]:
        raise AssertionError("must not be called when disabled")

    await asyncio.wait_for(_periodic_reconcile(pipeline, make_streams, 0), timeout=1)
