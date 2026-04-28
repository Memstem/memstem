"""ADR 0008 Tier 1 query log.

Records every search-result exposure (one row per surfaced memory) and
every ``memstem_get`` open into a bounded SQLite table. The hygiene
worker reads this log later to bump ``importance`` on records the user
actually retrieved.

Logging never breaks search. Every entry point is wrapped in a
``try/except`` that downgrades errors to a single warning and continues
— a corrupt log file or schema-version drift must not silently mute
``memstem_search``.

Three design constants worth knowing:

- **Storage:** the same ``_meta/index.db`` SQLite file as the rest of
  the index (table created in migration v5). Keeping it co-located
  means backups and snapshotting cover it without extra plumbing.
- **Boundedness:** rows are pruned by ``id`` once the row count exceeds
  ``max_rows`` (default 100k). The auto-increment ``id`` doubles as a
  monotonic insertion order so the hygiene worker can sweep "rows
  since last cursor" without timestamp comparisons.
- **Non-canonicality:** losing this table during a crash drifts
  importance back toward heuristic-only, which the rest of the system
  tolerates. We do not protect it as canonical state.

The recorded fields:

- ``ts`` — UTC ISO8601 wall clock at write time.
- ``kind`` — currently ``"search"`` (one row per hit) or ``"get"`` (one
  row per open). Future kinds (e.g., ``"upsert"``, ``"distill"``) can
  add without schema change.
- ``query`` — raw query string for searches; ``NULL`` for gets. Stored
  in the clear because (a) it's the user's own input on their own box
  and (b) the hygiene worker may want to debug "what query surfaced
  this record." Privacy-conscious users can disable logging entirely.
- ``client`` — origin label: ``"cli"``, ``"mcp"``, ``"http"``, etc.
  ``NULL`` is allowed when the call site is anonymous.
- ``memory_id`` — the surfaced memory's UUID. ``ON DELETE CASCADE``
  ensures the log doesn't credit deleted records.
- ``rank`` — 1-based position in the result list for searches; ``NULL``
  for gets.
- ``score`` — the post-importance final score for searches; ``NULL``
  for gets.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


DEFAULT_MAX_ROWS = 100_000
"""Default row cap for the ``query_log`` table. The hygiene worker
sweeps the table on a cadence and this cap is the upper bound between
sweeps. 100k is roughly 30 days of typical-vault traffic at 100
queries/day with 30 hits each. Tunable via ``HygieneConfig.query_log_max_rows``."""


@dataclass(frozen=True, slots=True)
class LoggedHit:
    """One memory exposure inside a search result list.

    Used as the input shape to :func:`log_search_results`. Constructed
    from a :class:`memstem.core.search.Result` at the search call site
    so the logger doesn't import the search module (avoids a cycle).
    """

    memory_id: str
    rank: int
    score: float


def log_search_results(
    db: sqlite3.Connection,
    *,
    query: str,
    hits: Iterable[LoggedHit],
    client: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    now: datetime | None = None,
) -> None:
    """Append one row per hit to the ``query_log`` table.

    Failures are logged at WARNING and swallowed — the search call site
    must not see logging exceptions.

    ``max_rows`` triggers a prune when the row count exceeds it after
    insert. Pruning deletes the oldest rows by id (FIFO).

    ``now`` is for tests; real callers should leave it at ``None`` and
    accept the wall-clock UTC timestamp.
    """
    timestamp = (now or datetime.now(tz=UTC)).isoformat()
    rows = [(timestamp, "search", query, client, h.memory_id, h.rank, h.score) for h in hits]
    if not rows:
        return
    try:
        with db:
            db.executemany(
                """
                INSERT INTO query_log (ts, kind, query, client, memory_id, rank, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            _maybe_prune(db, max_rows=max_rows)
    except sqlite3.Error as exc:
        # Common reasons: schema-version drift, foreign-key cascade race
        # (memory_id deleted between search and log), or a corrupted file.
        # None of those should silently mute search.
        logger.warning("query_log: failed to record search results: %s", exc)


def log_get(
    db: sqlite3.Connection,
    *,
    memory_id: str,
    client: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    now: datetime | None = None,
) -> None:
    """Record a ``memstem_get`` open. ``query``, ``rank``, and ``score`` are NULL."""
    timestamp = (now or datetime.now(tz=UTC)).isoformat()
    try:
        with db:
            db.execute(
                """
                INSERT INTO query_log (ts, kind, query, client, memory_id, rank, score)
                VALUES (?, ?, NULL, ?, ?, NULL, NULL)
                """,
                (timestamp, "get", client, memory_id),
            )
            _maybe_prune(db, max_rows=max_rows)
    except sqlite3.Error as exc:
        logger.warning("query_log: failed to record get for %s: %s", memory_id, exc)


def _maybe_prune(db: sqlite3.Connection, *, max_rows: int) -> None:
    """Trim the log to ``max_rows`` newest rows when over the cap.

    The 10% headroom keeps us from pruning on every single insert at
    steady-state — once we cross the cap we drop back below it by 10%
    so subsequent writes have room.
    """
    if max_rows <= 0:
        return
    row = db.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()
    if row is None:
        return
    count = int(row[0] if not hasattr(row, "keys") else row["n"])
    if count <= max_rows:
        return
    target = int(max_rows * 0.9)
    to_delete = count - target
    db.execute(
        """
        DELETE FROM query_log
        WHERE id IN (SELECT id FROM query_log ORDER BY id ASC LIMIT ?)
        """,
        (to_delete,),
    )


def count(db: sqlite3.Connection) -> int:
    """Total rows currently in the log (for tests + ``memstem doctor``)."""
    row = db.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()
    if row is None:
        return 0
    return int(row[0] if not hasattr(row, "keys") else row["n"])


__all__ = [
    "DEFAULT_MAX_ROWS",
    "LoggedHit",
    "count",
    "log_get",
    "log_search_results",
]
