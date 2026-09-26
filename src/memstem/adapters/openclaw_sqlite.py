"""Read-only OpenClaw SQLite trajectory discovery and bounded session replay.

Never use immutable=1: committed events can still live in OpenClaw's WAL.
There is no durable cursor to lose, or to advance before a pipeline write.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from memstem.adapters.base import MemoryRecord
from memstem.config import OpenClawWorkspace

logger = logging.getLogger(__name__)

SessionSignature = tuple[int, int, int, int]
"""(row count, first seq, last seq, total event bytes) for one session."""


def state_roots(ws: OpenClawWorkspace) -> list[Path]:
    return [(ws.path / p).expanduser().resolve() for p in ws.layout.trajectory_sqlite_roots]


def discover_databases(ws: OpenClawWorkspace) -> list[Path]:
    found: set[Path] = set()
    for root in state_roots(ws):
        if not root.is_dir():
            logger.warning("OpenClaw SQLite state root unavailable: %s", root)
            continue
        for path in root.glob("agents/*/agent/openclaw-agent.sqlite"):
            resolved = path.resolve()
            if resolved.is_relative_to(root) and resolved.is_file():
                found.add(resolved)
            else:
                logger.warning("OpenClaw SQLite database escapes configured root: %s", path)
    return sorted(found)


def database_fingerprint(path: Path) -> tuple[tuple[int, int, int, int], ...]:
    parts = []
    for file in (path, Path(str(path) + "-wal")):
        try:
            st = file.stat()
            parts.append((st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except FileNotFoundError:
            parts.append((0, 0, 0, 0))
    return tuple(parts)


def read_database(
    path: Path,
    ws: OpenClawWorkspace,
    *,
    known: Mapping[str, SessionSignature] | None = None,
    signatures_out: dict[str, SessionSignature] | None = None,
) -> list[MemoryRecord]:
    """Replay sessions from one OpenClaw SQLite database.

    ``known`` maps session ids to the signature seen on the previous
    poll; a session whose (row count, seq range, byte size) is unchanged
    is skipped without being parsed or emitted. The database file's
    fingerprint moves on every write, so without this each poll of an
    active agent re-emitted every session it had ever stored — hundreds
    of unchanged transcripts rewritten per pass. ``signatures_out`` is
    filled with the current signature of every session scanned so the
    caller can advance its memory only after the records are consumed.
    ``known=None`` (reconcile) replays everything.
    """
    # Local import avoids the adapter/parser import cycle.
    from memstem.adapters.openclaw import _parse_trajectory_lines

    records = []
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only = ON")
        # Stable snapshot for session inventory and rows, released before emission.
        db.execute("BEGIN")
        sessions = db.execute(
            "SELECT session_id, count(*), min(seq), max(seq), sum(length(CAST(event_json AS BLOB))) "
            "FROM trajectory_runtime_events GROUP BY session_id ORDER BY session_id"
        ).fetchall()
        for sid, count, first, last, size in sessions:
            # Session IDs become canonical vault filenames. Fail closed on paths.
            if (
                not isinstance(sid, str)
                or not sid
                or any(x in sid for x in ("/", "\\", ":"))
                or sid.startswith(".")
            ):
                logger.warning("invalid OpenClaw session id in %s: %r", path, sid)
                continue
            if first > 0 or count != last - first + 1:
                logger.warning(
                    "OpenClaw trajectory retention/gap %s session %s: seq=%s..%s rows=%s; "
                    "previously captured history is retained",
                    path,
                    sid,
                    first,
                    last,
                    count,
                )
            signature: SessionSignature = (count, first, last, size or 0)
            if signatures_out is not None:
                signatures_out[sid] = signature
            if known is not None and known.get(sid) == signature:
                continue
            cap = ws.layout.max_trajectory_bytes
            if cap and size > cap:
                logger.warning(
                    "skipping oversized SQLite trajectory %s session %s (%s > %s)",
                    path,
                    sid,
                    size,
                    cap,
                )
                continue
            rows = db.execute(
                "SELECT event_json FROM trajectory_runtime_events WHERE session_id=? ORDER BY seq",
                (sid,),
            )
            parsed = _parse_trajectory_lines((r[0] for r in rows), sid, preserve_history=True)
            if not parsed["body"]:
                continue
            records.append(
                MemoryRecord(
                    source="openclaw",
                    ref=f"{path}#session={sid}",
                    title=parsed["title"],
                    body=parsed["body"],
                    tags=[f"agent:{ws.tag}"],
                    metadata={
                        "type": "session",
                        "session_id": sid,
                        "created": parsed["first_timestamp"],
                        "updated": parsed["last_timestamp"],
                        "turn_count": parsed["turn_count"],
                        "trajectory_sqlite": {"first_seq": first, "last_seq": last, "rows": count},
                    },
                )
            )
    finally:
        db.close()
    return records
