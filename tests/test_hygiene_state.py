"""Tests for `memstem.hygiene.state` (ADR 0023 — in-daemon hygiene loop).

Cover: last-run timestamps round-trip, lock acquire/release semantics,
stale lock reclamation, due-for-run gating, and the ``snapshot`` view
used by ``/health``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memstem.core.index import Index
from memstem.hygiene.state import (
    ALL_STAGES,
    STAGE_DEDUP_JUDGE,
    STAGE_DISTILL_SESSIONS,
    STAGE_IMPORTANCE,
    acquire_stage_lock,
    due_for_run,
    get_last_run,
    get_lock_holder,
    release_stage_lock,
    set_last_run,
    snapshot,
)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


class TestLastRun:
    def test_get_last_run_missing_returns_none(self, index: Index) -> None:
        assert get_last_run(index.db, STAGE_IMPORTANCE) is None

    def test_set_and_get_round_trip(self, index: Index) -> None:
        ts = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, ts)
        assert get_last_run(index.db, STAGE_IMPORTANCE) == ts

    def test_set_overwrites_previous_value(self, index: Index) -> None:
        first = datetime(2026, 5, 1, tzinfo=UTC)
        second = datetime(2026, 5, 19, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, first)
        set_last_run(index.db, STAGE_IMPORTANCE, second)
        assert get_last_run(index.db, STAGE_IMPORTANCE) == second

    def test_per_stage_isolation(self, index: Index) -> None:
        ts = datetime(2026, 5, 19, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, ts)
        assert get_last_run(index.db, STAGE_DISTILL_SESSIONS) is None
        assert get_last_run(index.db, STAGE_DEDUP_JUDGE) is None

    def test_corrupt_value_returns_none(self, index: Index) -> None:
        # Defend against legacy rows or hand-edited values.
        index.db.execute(
            "INSERT INTO hygiene_state (key, value) VALUES (?, ?)",
            (f"last_run:{STAGE_IMPORTANCE}", "not-a-timestamp"),
        )
        index.db.commit()
        assert get_last_run(index.db, STAGE_IMPORTANCE) is None


class TestStageLock:
    def test_acquire_unheld_succeeds(self, index: Index) -> None:
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True
        # Lock holder timestamp is now set
        assert get_lock_holder(index.db, STAGE_IMPORTANCE) is not None

    def test_acquire_held_fails(self, index: Index) -> None:
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is False

    def test_release_then_reacquire(self, index: Index) -> None:
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True
        release_stage_lock(index.db, STAGE_IMPORTANCE)
        assert get_lock_holder(index.db, STAGE_IMPORTANCE) is None
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True

    def test_release_is_idempotent(self, index: Index) -> None:
        release_stage_lock(index.db, STAGE_IMPORTANCE)  # no-op when no lock
        release_stage_lock(index.db, STAGE_IMPORTANCE)  # safe to repeat

    def test_stale_lock_is_reclaimed(self, index: Index) -> None:
        # Plant a lock that's already older than the threshold.
        old = datetime.now(UTC) - timedelta(hours=2)
        index.db.execute(
            "INSERT INTO hygiene_state (key, value) VALUES (?, ?)",
            (f"running_since:{STAGE_IMPORTANCE}", old.isoformat()),
        )
        index.db.commit()
        # Default max_age_seconds=3600; 2h > 1h so this should reclaim.
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True

    def test_fresh_lock_is_not_reclaimed(self, index: Index) -> None:
        # Plant a recent lock (10 seconds old).
        recent = datetime.now(UTC) - timedelta(seconds=10)
        index.db.execute(
            "INSERT INTO hygiene_state (key, value) VALUES (?, ?)",
            (f"running_since:{STAGE_IMPORTANCE}", recent.isoformat()),
        )
        index.db.commit()
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is False

    def test_per_stage_locks_are_independent(self, index: Index) -> None:
        assert acquire_stage_lock(index.db, STAGE_IMPORTANCE) is True
        # Different stage can still acquire its own lock
        assert acquire_stage_lock(index.db, STAGE_DEDUP_JUDGE) is True


class TestDueForRun:
    def test_never_run_is_due(self, index: Index) -> None:
        assert due_for_run(index.db, STAGE_IMPORTANCE, 3600) is True

    def test_just_ran_is_not_due(self, index: Index) -> None:
        set_last_run(index.db, STAGE_IMPORTANCE, datetime.now(UTC))
        assert due_for_run(index.db, STAGE_IMPORTANCE, 3600) is False

    def test_elapsed_interval_makes_it_due(self, index: Index) -> None:
        old = datetime.now(UTC) - timedelta(seconds=7200)  # 2h ago
        set_last_run(index.db, STAGE_IMPORTANCE, old)
        assert due_for_run(index.db, STAGE_IMPORTANCE, 3600) is True

    def test_explicit_now_for_deterministic_tests(self, index: Index) -> None:
        anchor = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, anchor)
        assert (
            due_for_run(index.db, STAGE_IMPORTANCE, 3600, now=anchor + timedelta(seconds=59))
            is False
        )
        assert (
            due_for_run(index.db, STAGE_IMPORTANCE, 3600, now=anchor + timedelta(hours=2)) is True
        )


class TestSnapshot:
    def test_empty_vault_returns_all_stages_none(self, index: Index) -> None:
        snap = snapshot(index.db)
        assert set(snap["last_run"].keys()) == set(ALL_STAGES)
        for stage in ALL_STAGES:
            assert snap["last_run"][stage] is None
        assert snap["running"] == []

    def test_snapshot_reports_last_run(self, index: Index) -> None:
        ts = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, ts)
        snap = snapshot(index.db)
        assert snap["last_run"][STAGE_IMPORTANCE] == ts.isoformat()
        assert snap["last_run"][STAGE_DEDUP_JUDGE] is None

    def test_snapshot_reports_running(self, index: Index) -> None:
        acquire_stage_lock(index.db, STAGE_IMPORTANCE)
        snap = snapshot(index.db)
        assert STAGE_IMPORTANCE in snap["running"]
        release_stage_lock(index.db, STAGE_IMPORTANCE)
        assert STAGE_IMPORTANCE not in snapshot(index.db)["running"]
