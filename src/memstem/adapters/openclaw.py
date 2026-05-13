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
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter as fm
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.config import OpenClawWorkspace

logger = logging.getLogger(__name__)

DAILY_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
TRAJECTORY_SUFFIX = ".trajectory.jsonl"
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


def _format_turn(role: str, text: str) -> str:
    if not text.strip():
        return ""
    return f"**{role.title()}:** {text}"


def _parse_trajectory_file(path: Path) -> dict[str, Any] | None:
    """Parse an OpenClaw `*.trajectory.jsonl` into a transcript summary.

    Trajectory format is one JSON event per line. Two event types carry
    semantic conversation content:

    - ``prompt.submitted``: ``data.prompt`` is the user turn.
    - ``model.completed``: ``data.assistantTexts`` is a list of strings
      (the assistant's text responses).

    Other events (tool calls, context compilation, session boundary
    markers) are skipped — they're operational metadata that adds
    nothing to a search index.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None

    turns: list[str] = []
    title: str | None = None
    session_id: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    workspace_dir: str | None = None
    agent_id: str | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        ts = entry.get("ts")
        if isinstance(ts, str):
            if first_timestamp is None:
                first_timestamp = ts
            last_timestamp = ts

        # Capture session metadata from the first session.started event.
        if session_id is None:
            sid = entry.get("sessionId") or entry.get("traceId")
            if isinstance(sid, str):
                session_id = sid
        if workspace_dir is None:
            wd = entry.get("workspaceDir")
            if isinstance(wd, str):
                workspace_dir = wd
        if agent_id is None:
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            aid = data.get("agentId") if isinstance(data, dict) else None
            if isinstance(aid, str):
                agent_id = aid

        event_type = entry.get("type")
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}

        if event_type == "prompt.submitted":
            prompt = data.get("prompt") if isinstance(data, dict) else None
            if isinstance(prompt, str):
                turn = _format_turn("user", prompt)
                if turn:
                    turns.append(turn)
        elif event_type == "model.completed":
            assistant_texts = data.get("assistantTexts") if isinstance(data, dict) else None
            if isinstance(assistant_texts, list):
                joined = "\n\n".join(t for t in assistant_texts if isinstance(t, str) and t.strip())
                turn = _format_turn("assistant", joined)
                if turn:
                    turns.append(turn)

    if not session_id:
        # Trajectory filenames are `<id>.trajectory.jsonl`; strip both suffixes.
        session_id = path.name.removesuffix(TRAJECTORY_SUFFIX) or path.stem

    if title is None and turns:
        for turn in turns:
            if turn.startswith("**User:**"):
                title = turn[len("**User:** ") :].splitlines()[0][:80].strip()
                break
    if not title:
        title = f"session {session_id[:8]}"

    return {
        "session_id": session_id,
        "title": title,
        "body": "\n\n".join(turns),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "turn_count": len(turns),
        "workspace_dir": workspace_dir,
        "agent_id": agent_id,
    }


def _trajectory_to_record(path: Path, source_name: str = "openclaw") -> MemoryRecord | None:
    parsed = _parse_trajectory_file(path)
    if parsed is None:
        return None
    body = parsed["body"]
    if not isinstance(body, str) or not body.strip():
        return None

    mtime = _file_mtime_iso(path)
    metadata: dict[str, Any] = {
        "type": "session",
        "session_id": parsed["session_id"],
        "created": parsed["first_timestamp"] or mtime,
        "updated": parsed["last_timestamp"] or mtime,
        "turn_count": parsed["turn_count"],
    }
    if parsed["workspace_dir"]:
        metadata["workspace_dir"] = parsed["workspace_dir"]
    if parsed["agent_id"]:
        metadata["agent_id"] = parsed["agent_id"]

    return MemoryRecord(
        source=source_name,
        ref=str(path),
        title=str(parsed["title"]),
        body=body,
        tags=[],
        metadata=metadata,
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

    Honors the workspace's ``layout`` overrides — e.g. an agent that keeps
    memories under ``notes/`` instead of ``memory/`` will iterate through
    ``notes/**/*.md``. Top-level ``MEMORY.md``/``CLAUDE.md`` paths are also
    layout-configurable; setting either to ``None`` skips that file entirely.

    Session trajectory JSONLs (``*.trajectory.jsonl``) under
    ``layout.session_dirs`` are NOT yielded here — the caller routes them
    through ``_trajectory_to_record`` instead of the markdown reader.

    extra_tags annotate the role of top-level files beyond the agent tag:
    `core` for MEMORY.md, `instructions` for CLAUDE.md.
    """
    base = ws.path
    if not base.is_dir():
        return

    layout = ws.layout

    if layout.memory_md is not None:
        memory_md = base / layout.memory_md
        if memory_md.is_file():
            yield (memory_md, ["core"])

    if layout.claude_md is not None:
        claude_md = base / layout.claude_md
        if claude_md.is_file():
            yield (claude_md, ["instructions"])

    for rel_path in layout.extra_files:
        extra = base / rel_path
        if extra.is_file():
            yield (extra, [])

    for rel_dir in layout.memory_dirs:
        memory_dir = base / rel_dir
        if memory_dir.is_dir():
            for f in sorted(memory_dir.rglob("*.md")):
                if f.is_file():
                    yield (f, [])

    for rel_dir in layout.skills_dirs:
        skills_dir = base / rel_dir
        if skills_dir.is_dir():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                if f.is_file():
                    yield (f, [])


def _iter_workspace_trajectories(ws: OpenClawWorkspace) -> Iterator[Path]:
    """Yield ``*.trajectory.jsonl`` files from the workspace's session_dirs.

    Empty session_dirs (the default) yields nothing — trajectory ingestion
    is opt-in.
    """
    base = ws.path
    if not base.is_dir():
        return
    for rel_dir in ws.layout.session_dirs:
        session_dir = base / rel_dir
        if not session_dir.is_dir():
            continue
        for f in sorted(session_dir.rglob(f"*{TRAJECTORY_SUFFIX}")):
            if f.is_file():
                yield f


def _classify_workspace_path(path: Path, ws: OpenClawWorkspace) -> tuple[bool, list[str]]:
    """Return `(is_interesting, extra_tags)` for a path inside a workspace.

    Used by the watch loop to decide whether to emit a record on file change
    and to figure out the right role tags for top-level files. Mirrors the
    layout overrides used by ``_iter_workspace_files``.

    Trajectory JSONLs are matched here too but routed via the trajectory
    classifier — see ``_classify_trajectory_path``.
    """
    base = ws.path.resolve()
    try:
        path = path.resolve()
        path.relative_to(base)
    except (ValueError, OSError):
        return (False, [])

    layout = ws.layout

    if layout.memory_md is not None and path == (base / layout.memory_md).resolve():
        return (True, ["core"])
    if layout.claude_md is not None and path == (base / layout.claude_md).resolve():
        return (True, ["instructions"])

    for rel_path in layout.extra_files:
        try:
            if path == (base / rel_path).resolve():
                return (True, [])
        except OSError:
            continue

    if path.suffix == ".md":
        for rel_dir in layout.memory_dirs:
            memory_root = (base / rel_dir).resolve()
            if memory_root in path.parents:
                return (True, [])

    if path.name == "SKILL.md":
        for rel_dir in layout.skills_dirs:
            skills_root = (base / rel_dir).resolve()
            if skills_root in path.parents:
                return (True, [])

    return (False, [])


def _classify_trajectory_path(path: Path, ws: OpenClawWorkspace) -> bool:
    """Return True if `path` is a trajectory JSONL inside one of the
    workspace's `session_dirs`.

    Separate from `_classify_workspace_path` because trajectories use a
    different reader (``_trajectory_to_record``) instead of the markdown
    reader.
    """
    if not path.name.endswith(TRAJECTORY_SUFFIX):
        return False
    base = ws.path.resolve()
    try:
        path = path.resolve()
        path.relative_to(base)
    except (ValueError, OSError):
        return False
    for rel_dir in ws.layout.session_dirs:
        session_root = (base / rel_dir).resolve()
        if session_root in path.parents:
            return True
    return False


def _apply_workspace_tags(record: MemoryRecord, ws_tag: str, extra_tags: list[str]) -> MemoryRecord:
    new_tags = [*record.tags, f"agent:{ws_tag}", *extra_tags]
    return record.model_copy(update={"tags": new_tags})


def _apply_shared_tag(record: MemoryRecord) -> MemoryRecord:
    return record.model_copy(update={"tags": [*record.tags, "shared"]})


class _EventHandler(FileSystemEventHandler):
    """Coalesces rapid-fire file events via per-path debounce timers.

    Active sessions and growing daily logs often save 20-50 times before
    quiescing; without debouncing, each save triggers a full re-read,
    re-chunk, and re-embed of the whole file. The debounce window collapses
    those bursts into one enqueue per file once writes settle.

    Override the default 30-second window via the
    ``MEMSTEM_WATCH_DEBOUNCE_SECONDS`` environment variable, read at
    instance construction. Setting it to 0 restores the previous
    fire-on-every-event behaviour (useful in tests).
    """

    DEFAULT_DEBOUNCE_SECONDS = 30.0

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Path],
    ) -> None:
        super().__init__()
        self._loop = loop
        self._queue = queue
        self._pending: dict[Path, asyncio.TimerHandle] = {}
        self._debounce_seconds = float(
            os.environ.get(
                "MEMSTEM_WATCH_DEBOUNCE_SECONDS",
                str(self.DEFAULT_DEBOUNCE_SECONDS),
            )
        )

    def _enqueue(self, src: str) -> None:
        path = Path(src)
        # Markdown for memory/skill/instructions; *.trajectory.jsonl for
        # session trajectories (filtered further by _classify_*_path).
        if path.suffix != ".md" and not path.name.endswith(TRAJECTORY_SUFFIX):
            return
        if self._debounce_seconds <= 0:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, path)
            return
        self._loop.call_soon_threadsafe(self._schedule, path)

    def _schedule(self, path: Path) -> None:
        prior = self._pending.get(path)
        if prior is not None:
            prior.cancel()
        self._pending[path] = self._loop.call_later(self._debounce_seconds, self._fire, path)

    def _fire(self, path: Path) -> None:
        self._pending.pop(path, None)
        self._queue.put_nowait(path)

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
            for traj_path in _iter_workspace_trajectories(ws):
                traj_record = _trajectory_to_record(traj_path, self.name)
                if traj_record is None:
                    continue
                yield _apply_workspace_tags(traj_record, ws.tag, [])
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
            if _classify_trajectory_path(changed, ws):
                traj_record = _trajectory_to_record(changed, self.name)
                if traj_record is not None:
                    yield _apply_workspace_tags(traj_record, ws.tag, [])
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
