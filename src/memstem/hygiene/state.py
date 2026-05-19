"""hygiene_state helpers — last-run timestamps and per-stage locks (ADR 0023).

``hygiene_state`` is a small key/value table inside ``_meta/index.db``
already used by :mod:`memstem.hygiene.importance` for cursor tracking.
ADR 0023's in-daemon loop reuses the same table to coordinate with the
existing CLI hygiene commands, so both code paths see a single source
of truth.

Two key namespaces are introduced here:

- ``last_run:<stage>``     RFC 3339 timestamp of the last successful run.
- ``running_since:<stage>`` RFC 3339 timestamp set when a runner
  acquires the stage. Cleared on completion. A lock older than
  ``max_age_seconds`` is treated as crashed and reclaimed.

Stage names are the canonical short identifiers: ``distill_sessions``,
``dedup_judge``, ``importance``, ``project_records``. Add new stages
here as ADRs land.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TypedDict

logger = logging.getLogger(__name__)


class HygieneSnapshot(TypedDict):
    """Shape returned by :func:`snapshot` — JSON-ready for ``/health``."""

    last_run: dict[str, str | None]
    running: list[str]


STAGE_DISTILL_SESSIONS = "distill_sessions"
STAGE_DEDUP_JUDGE = "dedup_judge"
STAGE_IMPORTANCE = "importance"
STAGE_PROJECT_RECORDS = "project_records"

ALL_STAGES = (
    STAGE_DISTILL_SESSIONS,
    STAGE_DEDUP_JUDGE,
    STAGE_IMPORTANCE,
    STAGE_PROJECT_RECORDS,
)


def _last_run_key(stage: str) -> str:
    return f"last_run:{stage}"


def _lock_key(stage: str) -> str:
    return f"running_since:{stage}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # Tolerate legacy rows — treat as absent rather than crashing the loop.
        return None


def get_last_run(db: sqlite3.Connection, stage: str) -> datetime | None:
    """Return the timestamp of the last successful run, or ``None``."""
    row = db.execute(
        "SELECT value FROM hygiene_state WHERE key = ?",
        (_last_run_key(stage),),
    ).fetchone()
    return _parse_iso(row[0]) if row else None


def set_last_run(db: sqlite3.Connection, stage: str, ts: datetime) -> None:
    """Record a successful run."""
    db.execute(
        """
        INSERT INTO hygiene_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_last_run_key(stage), ts.isoformat()),
    )
    db.commit()


def get_lock_holder(db: sqlite3.Connection, stage: str) -> datetime | None:
    """Return the timestamp recorded when the current lock was acquired,
    or ``None`` if no runner currently holds the lock."""
    row = db.execute(
        "SELECT value FROM hygiene_state WHERE key = ?",
        (_lock_key(stage),),
    ).fetchone()
    return _parse_iso(row[0]) if row else None


def acquire_stage_lock(
    db: sqlite3.Connection,
    stage: str,
    *,
    max_age_seconds: int = 3600,
    now: datetime | None = None,
) -> bool:
    """Try to acquire the per-stage lock.

    Returns ``True`` if the caller now holds the lock, ``False`` if
    another runner does. A lock older than ``max_age_seconds`` is
    treated as crashed — the function reclaims it and returns ``True``.

    Atomic via ``INSERT OR ROLLBACK``: two concurrent callers can race
    but only one wins.
    """
    now = now or datetime.now(UTC)
    existing = get_lock_holder(db, stage)
    if existing is not None:
        age = (now - existing).total_seconds()
        if age < max_age_seconds:
            return False
        logger.warning(
            "hygiene[%s]: reclaiming stale lock (age=%.0fs > max_age=%ds)",
            stage,
            age,
            max_age_seconds,
        )

    db.execute(
        """
        INSERT INTO hygiene_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_lock_key(stage), now.isoformat()),
    )
    db.commit()
    return True


def release_stage_lock(db: sqlite3.Connection, stage: str) -> None:
    """Release the per-stage lock. Idempotent."""
    db.execute(
        "DELETE FROM hygiene_state WHERE key = ?",
        (_lock_key(stage),),
    )
    db.commit()


def due_for_run(
    db: sqlite3.Connection,
    stage: str,
    interval_seconds: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Has ``interval_seconds`` elapsed since the last successful run?

    Returns ``True`` if the stage has never run before, or if the
    elapsed wall-clock time is at least ``interval_seconds``.
    """
    last = get_last_run(db, stage)
    if last is None:
        return True
    now = now or datetime.now(UTC)
    return (now - last) >= timedelta(seconds=interval_seconds)


def snapshot(db: sqlite3.Connection) -> HygieneSnapshot:
    """Return a JSON-ready view of all stages — for ``/health``."""
    return HygieneSnapshot(
        last_run={
            stage: (ts.isoformat() if (ts := get_last_run(db, stage)) is not None else None)
            for stage in ALL_STAGES
        },
        running=[stage for stage in ALL_STAGES if get_lock_holder(db, stage) is not None],
    )


__all__ = [
    "ALL_STAGES",
    "STAGE_DEDUP_JUDGE",
    "STAGE_DISTILL_SESSIONS",
    "STAGE_IMPORTANCE",
    "STAGE_PROJECT_RECORDS",
    "HygieneSnapshot",
    "acquire_stage_lock",
    "due_for_run",
    "get_last_run",
    "get_lock_holder",
    "release_stage_lock",
    "set_last_run",
    "snapshot",
]
