"""Deterministic importance-bump worker (ADR 0008 Tier 1, PR-C).

Reads the bounded ``query_log`` written by the search/get path (PR-B)
and proposes conservative ``importance`` bumps for memories that
recently appeared in retrievals. The pass is split into a pure
``compute_importance_updates`` planner and a side-effecting
``apply_importance_updates`` writer so callers (CLI ``--dry-run``, the
daemon's hygiene loop) can preview before persisting.

Design:

- **Inputs:** the ``query_log`` table since the last cursor stored in
  ``hygiene_state``. The cursor advances only on ``--apply`` so a
  ``--dry-run`` is always re-runnable.
- **Per-record formula** (deliberately small — see ADR 0008's
  "tiebreaker, not a forcing function" framing):

    bump = sum_over_log_rows(
        per_row_weight(kind, rank, age_days)
    )
    new_importance = min(1.0, current + min(MAX_BUMP_PER_RUN, bump))

  where:

    per_row_weight =
        kind=get      : GET_WEIGHT * recency
        kind=search   : (SEARCH_WEIGHT_ROOT / max(rank, 1)) * recency

    recency =
        1.0 if age_days <= RECENT_DAYS
        0.5 otherwise

- **Caps:**
    - ``MAX_BUMP_PER_RUN`` (default 0.1) caps any single record's
      bump in a single sweep, so even an aggressive retrieval streak
      can't spike a record from 0.0 to 1.0 overnight.
    - The result is capped at 1.0 (importance domain).
    - The bump is non-decreasing — this pass never lowers importance.
      Decay is a separate concern.

- **Skip cases:** records whose ``valid_to`` is in the past
  (expired), or whose ``deprecated_by`` field is set (superseded). The
  hygiene worker should not credit phased-out content.

- **Idempotence:** the cursor in ``hygiene_state`` advances on apply
  so re-running the same sweep is a no-op. Dry-runs do not advance
  the cursor.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from memstem.core.frontmatter import Frontmatter
from memstem.core.index import Index
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)


GET_WEIGHT = 0.05
"""Per-row contribution for a ``memstem_get`` open. The user explicitly
asked for this record by id/path — strong signal."""

SEARCH_WEIGHT_ROOT = 0.01
"""Per-row contribution for a search exposure at rank 1. Higher ranks
divide this weight (rank 2 gets 0.005, rank 10 gets 0.001). Weighting
by ``1/rank`` mirrors how RRF treats lists — the top hit matters most."""

RECENT_DAYS = 30
"""Exposures within this window count at full weight. Older exposures
count at half — recent retrievals reflect current relevance."""

MAX_BUMP_PER_RUN = 0.1
"""Cap on the importance increase a single record can earn in one
sweep. Prevents runaway inflation and keeps bumps in the
"tiebreaker" magnitude ADR 0008 specified."""

CURSOR_KEY = "importance_last_query_log_id"


@dataclass(frozen=True)
class ImportanceUpdate:
    """One proposed importance bump.

    The ``reasons`` list lets ``--dry-run`` print why each bump was
    proposed — useful when tuning weights or debugging "why did this
    record jump."
    """

    memory_id: str
    current: float
    proposed: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportancePlan:
    """Full sweep result, returned by :func:`compute_importance_updates`."""

    updates: list[ImportanceUpdate]
    last_seen_id: int
    """The id of the most recent ``query_log`` row considered. Apply
    advances the cursor to this value; a dry-run leaves it untouched."""


def _read_cursor(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM hygiene_state WHERE key = ?", (CURSOR_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"] if hasattr(row, "keys") else row[0])
    except (TypeError, ValueError):
        return 0


def _write_cursor(db: sqlite3.Connection, value: int) -> None:
    db.execute(
        """
        INSERT INTO hygiene_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (CURSOR_KEY, str(value)),
    )


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _per_row_weight(kind: str, rank: int | None, age_days: float) -> float:
    """Score one log row's contribution to a record's bump.

    Returns 0.0 for unknown kinds so the planner is forward-compatible
    with future ``kind`` values (e.g., ``upsert``, ``distill``)."""
    recency = 1.0 if age_days <= RECENT_DAYS else 0.5
    if kind == "get":
        return GET_WEIGHT * recency
    if kind == "search":
        if rank is None or rank < 1:
            return 0.0
        return (SEARCH_WEIGHT_ROOT / rank) * recency
    return 0.0


def _is_skip(memory: Memory, now: datetime) -> str | None:
    """Return a human-readable reason to skip, or ``None`` if eligible."""
    fm = memory.frontmatter
    if fm.deprecated_by is not None:
        return f"deprecated_by={fm.deprecated_by}"
    if fm.valid_to is not None and fm.valid_to <= now:
        return f"expired (valid_to={fm.valid_to.isoformat()})"
    return None


def _current_importance(fm: Frontmatter) -> float:
    """The neutral default mirrors :data:`memstem.core.search.DEFAULT_IMPORTANCE`."""
    if fm.importance is None:
        return 0.5
    return fm.importance


def compute_importance_updates(
    vault: Vault,
    index: Index,
    *,
    now: datetime | None = None,
) -> ImportancePlan:
    """Plan the next importance-bump pass without mutating anything.

    Reads ``query_log`` rows newer than the stored cursor, aggregates
    per-memory weights, looks up each memory in the vault to apply the
    skip rules, and returns a list of proposed updates plus the cursor
    value an apply pass would advance to. Safe to call repeatedly —
    no side effects.
    """
    moment = now or datetime.now(tz=UTC)
    cursor = _read_cursor(index.db)
    rows = index.db.execute(
        """
        SELECT id, ts, kind, memory_id, rank
        FROM query_log
        WHERE id > ?
        ORDER BY id ASC
        """,
        (cursor,),
    ).fetchall()
    if not rows:
        return ImportancePlan(updates=[], last_seen_id=cursor)

    last_seen_id = max(int(r["id"]) for r in rows)

    # Aggregate weights per memory_id and capture detailed reasons.
    per_memory_weight: dict[str, float] = {}
    per_memory_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = row["kind"]
        ts = _parse_iso(row["ts"])
        if ts is None:
            continue
        age_days = max(0.0, (moment - ts).total_seconds() / 86400.0)
        weight = _per_row_weight(kind, row["rank"], age_days)
        if weight <= 0.0:
            continue
        memory_id = row["memory_id"]
        per_memory_weight[memory_id] = per_memory_weight.get(memory_id, 0.0) + weight
        counts = per_memory_counts.setdefault(memory_id, {"get": 0, "search": 0})
        if kind in counts:
            counts[kind] += 1

    updates: list[ImportanceUpdate] = []
    for memory_id, raw_bump in per_memory_weight.items():
        path_row = index.db.execute(
            "SELECT path FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if path_row is None:
            # Memory was deleted between log-write and hygiene; the
            # ON DELETE CASCADE on query_log normally handles this, but
            # log/cascade is best-effort and we shouldn't crash on a
            # gap. Just skip.
            continue
        try:
            memory = vault.read(path_row["path"])
        except MemoryNotFoundError:
            continue

        skip = _is_skip(memory, moment)
        if skip is not None:
            logger.debug("hygiene.importance: skipping %s (%s)", memory_id, skip)
            continue

        current = _current_importance(memory.frontmatter)
        if current >= 1.0:
            # Already pinned at the cap; no room to bump.
            continue
        bump = min(MAX_BUMP_PER_RUN, raw_bump)
        proposed = min(1.0, current + bump)
        if proposed <= current:
            continue

        counts = per_memory_counts.get(memory_id, {})
        reason_parts: list[str] = []
        if counts.get("search"):
            reason_parts.append(f"{counts['search']} search exposure(s)")
        if counts.get("get"):
            reason_parts.append(f"{counts['get']} get(s)")
        reason_parts.append(f"raw bump {raw_bump:.4f} (capped at {bump:.4f})")
        updates.append(
            ImportanceUpdate(
                memory_id=memory_id,
                current=current,
                proposed=proposed,
                reasons=reason_parts,
            )
        )

    # Sort biggest bumps first — useful for human review in --dry-run.
    updates.sort(key=lambda u: u.proposed - u.current, reverse=True)
    return ImportancePlan(updates=updates, last_seen_id=last_seen_id)


def apply_importance_updates(
    vault: Vault,
    index: Index,
    plan: ImportancePlan,
) -> int:
    """Persist the planned bumps and advance the cursor.

    Returns the count of memories actually updated. Memories that fail
    a vault re-read between plan and apply are logged and skipped — we
    don't crash the whole sweep on one missing file.

    The cursor advances even when the update list is empty, so a sweep
    over a window with no usable rows still moves forward. Without
    that, the next sweep would re-process the same rows.
    """
    n = 0
    for update in plan.updates:
        path_row = index.db.execute(
            "SELECT path FROM memories WHERE id = ?", (update.memory_id,)
        ).fetchone()
        if path_row is None:
            logger.warning(
                "hygiene.importance: memory %s vanished before apply; skipping",
                update.memory_id,
            )
            continue
        try:
            memory = vault.read(path_row["path"])
        except MemoryNotFoundError:
            logger.warning(
                "hygiene.importance: vault file for %s missing; skipping",
                update.memory_id,
            )
            continue

        # Re-check skip rules at apply time — between plan and apply, a
        # record may have been superseded or expired.
        if _is_skip(memory, datetime.now(tz=UTC)) is not None:
            continue

        # Build a new Memory with the bumped importance. Pydantic
        # `model_copy` keeps the rest of the frontmatter intact.
        new_fm = memory.frontmatter.model_copy(update={"importance": update.proposed})
        new_memory = Memory(frontmatter=new_fm, body=memory.body, path=memory.path)
        vault.write(new_memory)
        index.upsert(new_memory)
        n += 1

    # Advance cursor under the same lock as the pipeline writes for
    # consistency. _write_cursor is idempotent — re-running is fine.
    with index._lock, index.db:
        _write_cursor(index.db, plan.last_seen_id)
    return n


def reset_cursor(index: Index) -> None:
    """Reset the hygiene cursor to 0 so the next plan re-scans from the start.

    Useful for tests and for one-off recoveries; not exposed via CLI
    by default because it can amplify already-applied bumps.
    """
    with index._lock, index.db:
        index.db.execute("DELETE FROM hygiene_state WHERE key = ?", (CURSOR_KEY,))


def _normalize_memory_id(memory_id: UUID | str) -> str:
    return str(memory_id)


__all__ = [
    "CURSOR_KEY",
    "GET_WEIGHT",
    "MAX_BUMP_PER_RUN",
    "RECENT_DAYS",
    "SEARCH_WEIGHT_ROOT",
    "ImportancePlan",
    "ImportanceUpdate",
    "apply_importance_updates",
    "compute_importance_updates",
    "reset_cursor",
]
