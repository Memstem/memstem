"""OpenClaw / Ari filesystem adapter.

Reads markdown files belonging to one or more OpenClaw agent workspaces.
By convention each workspace has the layout:

    ~/<agent>/
      MEMORY.md             # always-loaded core
      CLAUDE.md             # per-agent operational rules
      memory/*.md           # daily logs + structured topics
      skills/<slug>/SKILL.md

`OpenClawWorkspace(path, tag)` configures one workspace; records emitted
from it get an `agent:<tag>` tag so cross-agent searches can filter or
group cleanly. `shared_files` cover agent-agnostic content like
HARD-RULES.md and get a `shared` tag instead.

Constructed with no workspaces, the adapter falls back to legacy "walk
every markdown file under each path you pass to reconcile/watch" mode —
this preserves the v0.1 behavior and keeps existing callers working.
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
from memstem.config import OpenClawWorkspace

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


def _iter_workspace_files(ws: OpenClawWorkspace) -> Iterator[tuple[Path, list[str]]]:
    """Yield `(file, extra_tags)` for the conventional locations in a workspace.

    extra_tags annotate the role of top-level files beyond the agent tag:
    `core` for MEMORY.md, `instructions` for CLAUDE.md.
    """
    base = ws.path
    if not base.is_dir():
        return

    memory_md = base / "MEMORY.md"
    if memory_md.is_file():
        yield (memory_md, ["core"])
    claude_md = base / "CLAUDE.md"
    if claude_md.is_file():
        yield (claude_md, ["instructions"])

    memory_dir = base / "memory"
    if memory_dir.is_dir():
        for f in sorted(memory_dir.rglob("*.md")):
            if f.is_file():
                yield (f, [])

    skills_dir = base / "skills"
    if skills_dir.is_dir():
        for f in sorted(skills_dir.rglob("SKILL.md")):
            if f.is_file():
                yield (f, [])


def _classify_workspace_path(path: Path, ws: OpenClawWorkspace) -> tuple[bool, list[str]]:
    """Return `(is_interesting, extra_tags)` for a path inside a workspace.

    Used by the watch loop to decide whether to emit a record on file change
    and to figure out the right role tags for top-level files.
    """
    base = ws.path.resolve()
    try:
        path = path.resolve()
        path.relative_to(base)
    except (ValueError, OSError):
        return (False, [])

    if path == base / "MEMORY.md":
        return (True, ["core"])
    if path == base / "CLAUDE.md":
        return (True, ["instructions"])
    if path.suffix == ".md" and (base / "memory") in path.parents:
        return (True, [])
    if path.name == "SKILL.md" and (base / "skills") in path.parents:
        return (True, [])
    return (False, [])


def _apply_workspace_tags(record: MemoryRecord, ws_tag: str, extra_tags: list[str]) -> MemoryRecord:
    new_tags = [*record.tags, f"agent:{ws_tag}", *extra_tags]
    return record.model_copy(update={"tags": new_tags})


def _apply_shared_tag(record: MemoryRecord) -> MemoryRecord:
    return record.model_copy(update={"tags": [*record.tags, "shared"]})


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
    """Reads OpenClaw markdown files into normalized `MemoryRecord` objects.

    Two modes:
    - **Workspace mode** (preferred): pass `workspaces=[...]` and optional
      `shared_files=[...]` to the constructor. Reconcile and watch then
      walk per-agent conventional layouts and tag accordingly.
    - **Legacy mode**: no constructor args; reconcile and watch use the
      `paths` argument as before, walking every markdown file. Kept for
      callers and tests that pre-date workspace mode.
    """

    name = "openclaw"

    def __init__(
        self,
        workspaces: list[OpenClawWorkspace] | None = None,
        shared_files: list[Path] | None = None,
    ) -> None:
        self.workspaces = list(workspaces) if workspaces else []
        self.shared_files = list(shared_files) if shared_files else []

    @property
    def _has_workspace_config(self) -> bool:
        return bool(self.workspaces or self.shared_files)

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        if self._has_workspace_config:
            async for record in self._reconcile_workspaces():
                yield record
            return
        for root in paths:
            for path in _iter_markdown_files(root):
                legacy_record = _file_to_record(path, self.name)
                if legacy_record is not None:
                    yield legacy_record

    async def _reconcile_workspaces(self) -> AsyncGenerator[MemoryRecord, None]:
        for ws in self.workspaces:
            for path, extra_tags in _iter_workspace_files(ws):
                ws_record = _file_to_record(path, self.name)
                if ws_record is None:
                    continue
                yield _apply_workspace_tags(ws_record, ws.tag, extra_tags)
        for shared in self.shared_files:
            if not shared.is_file():
                continue
            shared_record = _file_to_record(shared, self.name)
            if shared_record is None:
                continue
            yield _apply_shared_tag(shared_record)

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        queue: asyncio.Queue[Path] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        observer = Observer()
        handler = _EventHandler(loop=loop, queue=queue)

        watch_roots = self._watch_roots(paths)
        for root in watch_roots:
            if root.exists():
                observer.schedule(handler, str(root), recursive=True)
        observer.start()
        try:
            while True:
                changed = await queue.get()
                if not changed.is_file():
                    continue
                async for record in self._records_for_changed_path(changed, paths):
                    yield record
        finally:
            observer.stop()
            observer.join()

    def _watch_roots(self, fallback_paths: list[Path]) -> list[Path]:
        if not self._has_workspace_config:
            return list(fallback_paths)
        roots: set[Path] = set()
        for ws in self.workspaces:
            roots.add(ws.path)
        for shared in self.shared_files:
            roots.add(shared.parent)
        return list(roots)

    async def _records_for_changed_path(
        self, changed: Path, fallback_paths: list[Path]
    ) -> AsyncGenerator[MemoryRecord, None]:
        if not self._has_workspace_config:
            record = _file_to_record(changed, self.name)
            if record is not None:
                yield record
            return

        for ws in self.workspaces:
            interesting, extra_tags = _classify_workspace_path(changed, ws)
            if interesting:
                record = _file_to_record(changed, self.name)
                if record is not None:
                    yield _apply_workspace_tags(record, ws.tag, extra_tags)
                return

        for shared in self.shared_files:
            try:
                if changed.resolve() == shared.resolve():
                    record = _file_to_record(changed, self.name)
                    if record is not None:
                        yield _apply_shared_tag(record)
                    return
            except OSError:
                continue


__all__ = ["OpenClawAdapter"]
