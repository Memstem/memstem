"""Registry of source files the adapters could not ingest.

A memory or skill file whose YAML frontmatter does not parse (or that cannot
be read) is logged at WARNING by the adapter and skipped. Until 0.24.0 that
was the only trace: the file sat outside the index — invisible to search,
``doctor``, ``stat`` and the daemon's ``/health`` — until somebody read a
startup log by eye (one such file went unnoticed for sixteen days on a
client workstation, reported 2026-09-17).

This process-wide registry remembers every such file, keyed by path, so the
daemon can expose ``skipped_files`` on ``/health`` and operators can see the
count and the paths. An adapter clears a path the next time it parses
cleanly, so a fixed file drops off the list on its own.

Not persisted: it reflects what this daemon process has seen since start.
Every daemon reconciles all its roots at startup, so the list is complete
within a minute or two of boot.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SkippedFiles:
    """Thread-safe path -> reason map (see module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, dict[str, Any]] = {}

    def record(
        self, path: Path | str, reason: object, *, source: str, kind: str = "frontmatter"
    ) -> None:
        key = str(path)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            prev = self._files.get(key)
            self._files[key] = {
                "path": key,
                "source": source,
                "kind": kind,
                "reason": " ".join(str(reason).split())[:300],
                "since": prev["since"] if prev else now,
                "last_seen": now,
            }

    def clear(self, path: Path | str) -> None:
        with self._lock:
            self._files.pop(str(path), None)

    def count(self) -> int:
        with self._lock:
            return len(self._files)

    def snapshot(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._files.values(), key=lambda f: (f["since"], f["path"]))
        return [dict(f) for f in (items if limit is None else items[:limit])]

    def reset(self) -> None:
        with self._lock:
            self._files.clear()


SKIPPED = SkippedFiles()
"""The one registry the adapters write to and ``/health`` reads."""
