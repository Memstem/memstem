"""Adapter base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class MemoryRecord(BaseModel):
    """A normalized memory record produced by an adapter."""

    source: str
    """Adapter name, e.g. 'claude-code', 'openclaw'."""

    ref: str
    """Source-specific identifier (session id, file path, etc.)."""

    title: str | None = None
    body: str
    tags: list[str] = []
    metadata: dict[str, Any] = {}


class Adapter(ABC):
    """Base class for all Memstem adapters.

    Adapters are responsible for watching one external AI's filesystem
    and producing normalized MemoryRecord objects. Storage and indexing
    are downstream — adapters never touch the index directly.
    """

    name: str
    """Unique identifier, e.g. 'claude-code', 'openclaw'."""

    _observer: Any = None
    """The running ``watch()``'s watchdog observer, registered after
    ``observer.start()`` and cleared on shutdown. Read via
    :meth:`watcher_alive`; never touched by callers directly."""

    @abstractmethod
    def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        """Yield records as files change. Long-running async generator."""
        ...

    @abstractmethod
    def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        """Yield records by scanning paths from scratch. One-shot async generator."""
        ...

    def source_exists(self, ref: str) -> bool:
        """Does the upstream source for this ``ref`` still exist on disk?

        Used by the source-deletion sweep (ADR 0026) as the liveness check
        for authored records (``memory`` / ``skill`` / ``daily``). For every
        current adapter a ref IS the on-disk source path (``ref=str(path)``),
        so the default is a plain file check. The sweep's *derived-vs-authored*
        guard is the record's ``type`` (a join in the index), NOT this method —
        a ``session`` ``.jsonl`` is just as file-backed as a memory, so
        ``source_exists`` must not be relied on to exclude it.

        Override only if a future adapter uses a non-path ref (a session id,
        a synthetic/aggregate key); such an adapter's authored records should
        either not be swept or return a meaningful liveness here.
        """
        return Path(ref).is_file()

    def source_roots(self) -> list[Path]:
        """Configured root directories this adapter ingests from (ADR 0026).

        The source-deletion sweep uses these to tell a real file deletion
        (the root still exists, a file under it is gone → tombstone) from a
        vanished/unmounted root (the root itself is gone → skip everything
        under it, never mass-tombstone). It also scopes the sweep's safety
        valve PER ROOT, so one workspace's bulk cleanup never blocks another.

        Default empty: adapters that don't declare roots fall back to a
        per-ref containing-directory heuristic in the sweep.
        """
        return []

    def watcher_alive(self) -> bool | None:
        """Liveness of this adapter's watchdog observer thread.

        ``None`` means no watch is running (``watch()`` not started yet,
        shut down cleanly, or it has nothing to observe). ``False`` means
        a watch IS running but its observer thread died — file events are
        silently being dropped; ``/health`` reports this as a
        ``watcher_dead:<name>`` problem. ``True`` is the healthy state.
        """
        observer = self._observer
        return None if observer is None else bool(observer.is_alive())
