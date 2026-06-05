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
from memstem.cli import _reconcile_all
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
