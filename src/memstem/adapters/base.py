"""Adapter base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

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
    metadata: dict = {}


class Adapter(ABC):
    """Base class for all Memstem adapters.

    Adapters are responsible for watching one external AI's filesystem
    and producing normalized MemoryRecord objects. Storage and indexing
    are downstream — adapters never touch the index directly.
    """

    name: str
    """Unique identifier, e.g. 'claude-code', 'openclaw'."""

    @abstractmethod
    async def watch(self, paths: list[Path]) -> AsyncIterator[MemoryRecord]:
        """Yield records as files change. Long-running."""
        ...

    @abstractmethod
    async def reconcile(self, paths: list[Path]) -> AsyncIterator[MemoryRecord]:
        """Yield records by scanning paths from scratch. One-shot."""
        ...
