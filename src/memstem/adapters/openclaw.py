"""OpenClaw / Ari filesystem adapter.

Watches the on-disk markdown files that Ari and other OpenClaw agents drop
into `~/ari/memory/`, `~/ari/skills/<slug>/SKILL.md`, and similar paths.
Files are already markdown, often without YAML frontmatter, so this adapter
mostly normalizes — it does not generate ids or write to the vault. That's
the consuming pipeline's job.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import frontmatter as fm
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from memstem.adapters.base import Adapter, MemoryRecord

logger = logging.getLogger(__name__)

DAILY_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _classify_type(path: Path) -> str:
    if path.name == "SKILL.md":
        return "skill"
    if DAILY_FILENAME_RE.match(path.name):
        return "daily"
    return "memory"


def _extract_h1(body: str) -> str | None:
    match = H1_RE.search(body)
    return match.group(1).strip() if match else None


def _file_mtime_iso(path: Path) -> str:
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return ts.isoformat()


def _file_to_record(path: Path, source_name: str) -> MemoryRecord | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None

    try:
        post = fm.loads(text)
    except Exception as exc:
        logger.warning("frontmatter parse failed for %s: %s", path, exc)
        return None

    meta = dict(post.metadata)
    body = post.content
    record_type = _classify_type(path)

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _extract_h1(body) or path.stem

    raw_tags = meta.get("tags", [])
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

    mtime_iso = _file_mtime_iso(path)
    created = meta.get("created") or mtime_iso
    updated = meta.get("updated") or mtime_iso

    return MemoryRecord(
        source=source_name,
        ref=str(path),
        title=title.strip() if isinstance(title, str) else None,
        body=body,
        tags=tags,
        metadata={
            "type": record_type,
            "created": str(created),
            "updated": str(updated),
            "raw_frontmatter": meta,
        },
    )


def _iter_markdown_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    if root.is_file():
        if root.suffix == ".md":
            yield root
        return
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            yield path


class _EventHandler(FileSystemEventHandler):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Path],
    ) -> None:
        super().__init__()
        self._loop = loop
        self._queue = queue

    def _enqueue(self, src: str) -> None:
        path = Path(src)
        if path.suffix != ".md":
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and getattr(event, "dest_path", None):
            self._enqueue(str(event.dest_path))


class OpenClawAdapter(Adapter):
    """Reads Ari/OpenClaw markdown files into normalized `MemoryRecord` objects."""

    name = "openclaw"

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        for root in paths:
            for path in _iter_markdown_files(root):
                record = _file_to_record(path, self.name)
                if record is not None:
                    yield record

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        queue: asyncio.Queue[Path] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        observer = Observer()
        handler = _EventHandler(loop=loop, queue=queue)
        for root in paths:
            if root.exists():
                observer.schedule(handler, str(root), recursive=True)
        observer.start()
        try:
            while True:
                path = await queue.get()
                if not path.is_file():
                    continue
                record = _file_to_record(path, self.name)
                if record is not None:
                    yield record
        finally:
            observer.stop()
            observer.join()


__all__ = ["OpenClawAdapter"]
