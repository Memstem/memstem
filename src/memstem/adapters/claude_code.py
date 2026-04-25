"""Claude Code session JSONL adapter.

Watches the JSONL session files Claude Code writes under
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Each file is one
conversation; this adapter folds it into a single per-session
`MemoryRecord` (type=session) carrying the chronological turn transcript.

For v0.1 the policy is "re-emit the full session on every file change."
The consuming pipeline upserts by `ref`, so re-emits idempotently replace
the prior record. A future version can track per-file line offsets to
emit incrementally — see PLAN step 5.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from memstem.adapters.base import Adapter, MemoryRecord

logger = logging.getLogger(__name__)


def _extract_text(content: Any) -> str:
    """Pull plain text out of a Claude message content payload.

    Content is either a bare string or a list of typed blocks (`text`,
    `tool_use`, `tool_result`, etc.). Tool blocks are summarized so the
    transcript stays readable but doesn't pull in raw tool I/O blobs.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block_type == "tool_use":
            name = block.get("name", "tool")
            parts.append(f"[tool_use: {name}]")
        elif block_type == "tool_result":
            parts.append("[tool_result]")
    return "\n".join(parts)


def _format_turn(role: str, text: str) -> str:
    if not text.strip():
        return ""
    return f"**{role.title()}:** {text}"


def _file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _parse_session_file(path: Path) -> dict[str, Any] | None:
    """Parse a session JSONL into a summary dict, or None if unreadable."""
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

        ts = entry.get("timestamp")
        if isinstance(ts, str):
            if first_timestamp is None:
                first_timestamp = ts
            last_timestamp = ts
        sid = entry.get("sessionId")
        if isinstance(sid, str) and session_id is None:
            session_id = sid

        entry_type = entry.get("type")
        if entry_type == "ai-title":
            candidate = entry.get("title") or entry.get("text")
            if isinstance(candidate, str) and candidate.strip():
                title = candidate.strip()
        elif entry_type in ("user", "assistant"):
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            text_payload = _extract_text(msg.get("content", ""))
            turn = _format_turn(entry_type, text_payload)
            if turn:
                turns.append(turn)

    if not session_id:
        session_id = path.stem
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
    }


def _session_to_record(path: Path, source_name: str = "claude-code") -> MemoryRecord | None:
    parsed = _parse_session_file(path)
    if parsed is None:
        return None
    body = parsed["body"]
    if not isinstance(body, str) or not body.strip():
        return None

    project_dir = path.parent.name
    tags = [project_dir.lstrip("-")] if project_dir.startswith("-") else []

    return MemoryRecord(
        source=source_name,
        ref=str(path),
        title=str(parsed["title"]),
        body=body,
        tags=tags,
        metadata={
            "type": "session",
            "session_id": parsed["session_id"],
            "created": parsed["first_timestamp"] or _file_mtime_iso(path),
            "updated": parsed["last_timestamp"] or _file_mtime_iso(path),
            "turn_count": parsed["turn_count"],
            "project": project_dir,
        },
    )


def _iter_jsonl_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    if root.is_file():
        if root.suffix == ".jsonl":
            yield root
        return
    for path in sorted(root.rglob("*.jsonl")):
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
        if path.suffix != ".jsonl":
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        if not event.is_directory and dest:
            self._enqueue(str(dest))


class ClaudeCodeAdapter(Adapter):
    """Reads Claude Code session JSONL files into per-session `MemoryRecord` objects."""

    name = "claude-code"

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        for root in paths:
            for path in _iter_jsonl_files(root):
                record = _session_to_record(path, self.name)
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
                record = _session_to_record(path, self.name)
                if record is not None:
                    yield record
        finally:
            observer.stop()
            observer.join()


__all__ = ["ClaudeCodeAdapter"]
