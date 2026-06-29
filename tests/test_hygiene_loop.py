"""Tests for `memstem.hygiene.loop` (ADR 0023).

Focused on the orchestration: interval gating, lock acquisition,
per-stage failure isolation, the ``loop_enabled`` short-circuit, and
clean cancellation. The individual stage runners are stubbed; the
correctness of each hygiene stage is covered by its own test module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memstem.config import HygieneConfig
from memstem.core.index import Index
from memstem.core.storage import Vault
from memstem.hygiene.loop import HygieneLoop
from memstem.hygiene.state import (
    STAGE_DISTILL_SESSIONS,
    STAGE_IMPORTANCE,
    STAGE_PROJECT_RECORDS,
    acquire_stage_lock,
    get_last_run,
    get_lock_holder,
    set_last_run,
)


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


def _fast_cfg(**overrides: object) -> HygieneConfig:
    """Build a HygieneConfig with all intervals short enough that every
    stage fires immediately when due. Stage-specific intervals can be
    overridden per test."""
    fields: dict[str, object] = {
        "loop_enabled": True,
        "loop_poll_interval_seconds": 1,
        "distill_interval_seconds": 1,
        "importance_interval_seconds": 1,
        "project_records_interval_seconds": 1,
        "summarizer_provider": "noop",
    }
    fields.update(overrides)
    # HygieneConfig has typed fields; pydantic's BaseModel accepts
    # arbitrary kwargs at runtime, so silence the mypy check on the
    # spread call. The model itself enforces the types.
    return HygieneConfig(**fields)  # type: ignore[arg-type]


# ─── loop_enabled short-circuit ───────────────────────────────────


@pytest.mark.asyncio
async def test_loop_disabled_returns_immediately(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg(loop_enabled=False)
    loop = HygieneLoop(vault, index, cfg)
    # If the loop honored the flag, run() returns; if not, it would
    # hang and the wait_for would time out.
    await asyncio.wait_for(loop.run(), timeout=1.0)


# ─── _maybe_run_stage orchestration ───────────────────────────────


@pytest.mark.asyncio
async def test_stage_runs_when_due(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg()
    loop = HygieneLoop(vault, index, cfg)
    fn = MagicMock()
    await loop._maybe_run_stage(STAGE_IMPORTANCE, interval_seconds=1, fn=fn)
    fn.assert_called_once()
    # last_run recorded
    assert get_last_run(index.db, STAGE_IMPORTANCE) is not None
    # lock released
    assert get_lock_holder(index.db, STAGE_IMPORTANCE) is None


@pytest.mark.asyncio
async def test_stage_skipped_if_not_due(vault: Vault, index: Index) -> None:
    set_last_run(index.db, STAGE_IMPORTANCE, datetime.now(UTC))
    cfg = _fast_cfg(importance_interval_seconds=3600)
    loop = HygieneLoop(vault, index, cfg)
    fn = MagicMock()
    await loop._maybe_run_stage(STAGE_IMPORTANCE, interval_seconds=3600, fn=fn)
    fn.assert_not_called()


@pytest.mark.asyncio
async def test_stage_skipped_when_lock_held(vault: Vault, index: Index) -> None:
    # Simulate another runner holding the lock.
    acquire_stage_lock(index.db, STAGE_IMPORTANCE)
    cfg = _fast_cfg()
    loop = HygieneLoop(vault, index, cfg)
    fn = MagicMock()
    await loop._maybe_run_stage(STAGE_IMPORTANCE, interval_seconds=1, fn=fn)
    fn.assert_not_called()
    # No last_run was recorded
    assert get_last_run(index.db, STAGE_IMPORTANCE) is None


@pytest.mark.asyncio
async def test_stage_failure_releases_lock(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg()
    loop = HygieneLoop(vault, index, cfg)

    def boom() -> None:
        raise RuntimeError("synthetic stage failure")

    await loop._maybe_run_stage(STAGE_IMPORTANCE, interval_seconds=1, fn=boom)
    # Failure logged + lock released
    assert get_lock_holder(index.db, STAGE_IMPORTANCE) is None
    # last_run NOT advanced on failure
    assert get_last_run(index.db, STAGE_IMPORTANCE) is None


@pytest.mark.asyncio
async def test_failure_in_one_stage_does_not_affect_others(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg()
    loop = HygieneLoop(vault, index, cfg)

    def boom() -> None:
        raise RuntimeError("boom")

    ok = MagicMock()

    await loop._maybe_run_stage(STAGE_IMPORTANCE, interval_seconds=1, fn=boom)
    await loop._maybe_run_stage(STAGE_PROJECT_RECORDS, interval_seconds=1, fn=ok)

    ok.assert_called_once()
    assert get_last_run(index.db, STAGE_PROJECT_RECORDS) is not None
    assert get_last_run(index.db, STAGE_IMPORTANCE) is None


# ─── _tick coverage ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_runs_all_stages(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg()
    loop = HygieneLoop(vault, index, cfg)
    with (
        patch.object(loop, "_run_importance") as p_imp,
        patch.object(loop, "_run_distill_sessions") as p_dist,
        patch.object(loop, "_run_project_records") as p_proj,
    ):
        await loop._tick()

    p_imp.assert_called_once()
    p_dist.assert_called_once()
    p_proj.assert_called_once()

    # Each stage's last_run is set
    for stage in (
        STAGE_IMPORTANCE,
        STAGE_DISTILL_SESSIONS,
        STAGE_PROJECT_RECORDS,
    ):
        assert get_last_run(index.db, stage) is not None


@pytest.mark.asyncio
async def test_tick_skips_recently_run_stages(vault: Vault, index: Index) -> None:
    # Mark importance as just-run; everything else is fresh.
    set_last_run(index.db, STAGE_IMPORTANCE, datetime.now(UTC))
    cfg = _fast_cfg(importance_interval_seconds=3600)
    loop = HygieneLoop(vault, index, cfg)
    with (
        patch.object(loop, "_run_importance") as p_imp,
        patch.object(loop, "_run_distill_sessions") as p_dist,
        patch.object(loop, "_run_project_records") as p_proj,
    ):
        await loop._tick()

    p_imp.assert_not_called()
    p_dist.assert_called_once()
    p_proj.assert_called_once()


# ─── Cancellation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_is_cancellable(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg(loop_poll_interval_seconds=1)
    loop = HygieneLoop(vault, index, cfg)
    # Stub all stages to do nothing so the loop just polls
    with (
        patch.object(loop, "_run_importance"),
        patch.object(loop, "_run_distill_sessions"),
        patch.object(loop, "_run_project_records"),
    ):
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ─── Summarizer lazy build ─────────────────────────────────────────


def test_summarizer_noop_built_lazily(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg(summarizer_provider="noop")
    loop = HygieneLoop(vault, index, cfg)
    assert loop._summarizer is None
    s = loop._get_summarizer()
    assert s is not None
    # Cached on second call
    assert loop._get_summarizer() is s


def test_unknown_summarizer_provider_records_reason(vault: Vault, index: Index) -> None:
    cfg = _fast_cfg(summarizer_provider="not-a-real-provider")
    loop = HygieneLoop(vault, index, cfg)
    assert loop._get_summarizer() is None
    assert loop._summarizer_unavailable_reason is not None
    assert "unknown summarizer provider" in loop._summarizer_unavailable_reason
