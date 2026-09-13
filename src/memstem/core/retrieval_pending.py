"""Bounded, durable overflow for retrieval telemetry during index write bursts.

Canonical memories are unaffected. Rows replay on the next successful log write.
Replay is at-least-once across crashes between DB commit and file removal.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
MAX_PENDING_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024
MAX_PENDING_FILES = 1000


def directory(db: sqlite3.Connection) -> Path | None:
    path = next((r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main"), "")
    return Path(path).parent / "query-log-pending" if path else None


def enqueue(db: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> bool:
    root = directory(db)
    if root is None:
        return False
    payload = json.dumps(rows).encode()
    if len(payload) > MAX_BATCH_BYTES:
        return False
    root.mkdir(mode=0o700, exist_ok=True)
    files = list(root.glob("*.json"))
    size = 0
    for path in files:
        try:
            size += path.stat().st_size
        except FileNotFoundError:
            pass  # another logger just replayed this batch
    if len(files) >= MAX_PENDING_FILES or size + len(payload) > MAX_PENDING_BYTES:
        return False
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(temporary.with_suffix(".json"))
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info("query_log: queued %d rows for replay after index writer contention", len(rows))
    return True


def replay(writer: sqlite3.Connection) -> list[Path]:
    root = directory(writer)
    if root is None or not root.is_dir():
        return []
    completed = []
    for path in sorted(root.glob("*.json"))[:100]:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_BATCH_BYTES:
                raise ValueError("invalid pending log file")
            rows = json.loads(path.read_text())
            if not isinstance(rows, list) or any(
                not isinstance(r, list) or len(r) != 7 for r in rows
            ):
                raise ValueError("invalid pending log rows")
            # A deleted memory should not strand the entire pending batch.
            writer.executemany(
                "INSERT INTO query_log(ts,kind,query,client,memory_id,rank,score) "
                "SELECT ?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM memories WHERE id=?)",
                [(*r, r[4]) for r in rows],
            )
            completed.append(path)
        except (OSError, ValueError) as exc:
            logger.warning("query_log: pending replay failed for %s: %s", path, exc)
    return completed


def remove_replayed(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("query_log: could not retire replayed batch %s: %s", path, exc)
