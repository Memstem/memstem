"""Codex (OpenAI) session, skill, and memory adapter.

Watches three roots under a configurable ``codex_home`` (default
``~/.codex``):

1. **Sessions** — ``sessions/YYYY/MM/DD/rollout-*.jsonl``. One JSONL
   per Codex session, partitioned by date. Folded into one
   ``MemoryRecord`` per file (``type=session``) with the chronological
   transcript.

2. **Skills** — ``skills/<name>/SKILL.md``. User-authored skills get
   one record each (``type=skill``). The ``.system/`` subdirectory
   (OpenAI-shipped skills like ``imagegen``, ``skill-creator``) is
   skipped by design — vendor content is not personal memory.

3. **Memories** — ``memories/*.md``. Free-form markdown user notes; one
   record per file (``type=memory``). Directory is often empty on a
   fresh Codex install; the watcher still subscribes so new files
   are picked up.

See ADR 0022 for the design rationale, the boilerplate filter rules
(``developer``-role messages and ``<environment_context>`` stubs are
dropped), and the rejected alternatives.
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

logger = logging.getLogger(__name__)

H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
ENV_CONTEXT_PREFIX = "<environment_context>"
PERMISSIONS_PREFIX = "<permissions instructions>"


def _file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _extract_h1(body: str) -> str | None:
    match = H1_RE.search(body)
    return match.group(1).strip() if match else None


def _slugify_cwd(cwd: str) -> str:
    """Turn a cwd into a project tag, matching Claude Code's convention.

    ``/home/ubuntu/memstem`` → ``home-ubuntu-memstem``. Leading slash
    stripped; remaining slashes replaced with hyphens; non-tag chars
    coerced to hyphens; leading/trailing hyphens trimmed. Mirrors the
    slug Claude Code embeds in its project directory names so
    cross-client searches group cleanly.
    """
    if not cwd:
        return ""
    stripped = cwd.strip("/")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stripped.replace("/", "-"))
    return slug.strip("-")


def _extract_message_text(content: Any) -> str:
    """Pull plain text out of a Codex message ``content`` payload.

    Codex messages carry a list of blocks; the meaningful ones are
    ``input_text`` (user / developer turns) and ``output_text``
    (assistant turns). Other shapes are dropped.
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
        if block_type in ("input_text", "output_text", "text"):
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _format_turn(role: str, text: str) -> str:
    if not text.strip():
        return ""
    return f"**{role.title()}:** {text}"


def _format_function_call(payload: dict[str, Any]) -> str:
    name = payload.get("name") or "function"
    return f"[function_call: {name}]"


def _parse_session_file(path: Path) -> dict[str, Any] | None:
    """Parse a Codex JSONL rollout into a summary dict.

    See ADR 0022 for the filter rules. The parse is defensive — any
    unrecognized shape is dropped silently rather than crashing the
    daemon.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None

    turns: list[str] = []
    session_id: str | None = None
    cwd: str | None = None
    cli_version: str | None = None
    model_provider: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    title: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
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

        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        if entry_type == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str):
                session_id = sid
            payload_cwd = payload.get("cwd")
            if isinstance(payload_cwd, str):
                cwd = payload_cwd
            v = payload.get("cli_version")
            if isinstance(v, str):
                cli_version = v
            mp = payload.get("model_provider")
            if isinstance(mp, str):
                model_provider = mp
            continue

        if entry_type != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role", "")
            if role == "developer":
                continue  # boilerplate permissions block
            content_text = _extract_message_text(payload.get("content"))
            if not content_text.strip():
                continue
            if content_text.lstrip().startswith(PERMISSIONS_PREFIX):
                continue
            if role == "user" and content_text.lstrip().startswith(ENV_CONTEXT_PREFIX):
                continue  # autogenerated env stub
            turn = _format_turn(role, content_text)
            if turn:
                turns.append(turn)
                if title is None and role == "user":
                    title = content_text.splitlines()[0][:80].strip()
        elif payload_type == "function_call":
            turns.append(_format_function_call(payload))
        elif payload_type == "function_call_output":
            turns.append("[function_call_output]")
        # reasoning / other types: drop

    if not session_id:
        session_id = path.stem
    if not title:
        title = f"codex session {session_id[:8]}"

    return {
        "session_id": session_id,
        "title": title,
        "body": "\n\n".join(turns),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "turn_count": len(turns),
        "cwd": cwd,
        "cli_version": cli_version,
        "model_provider": model_provider,
    }


def _session_to_record(path: Path, source_name: str = "codex") -> MemoryRecord | None:
    parsed = _parse_session_file(path)
    if parsed is None:
        return None
    body = parsed["body"]
    if not isinstance(body, str) or not body.strip():
        return None

    cwd = parsed.get("cwd")
    project_tag = _slugify_cwd(cwd) if isinstance(cwd, str) else ""
    tags = [project_tag] if project_tag else []

    metadata: dict[str, Any] = {
        "type": "session",
        "session_id": parsed["session_id"],
        "created": parsed["first_timestamp"] or _file_mtime_iso(path),
        "updated": parsed["last_timestamp"] or _file_mtime_iso(path),
        "turn_count": parsed["turn_count"],
    }
    if cwd:
        metadata["cwd"] = cwd
    if parsed.get("cli_version"):
        metadata["cli_version"] = parsed["cli_version"]
    if parsed.get("model_provider"):
        metadata["model_provider"] = parsed["model_provider"]

    return MemoryRecord(
        source=source_name,
        ref=str(path),
        title=str(parsed["title"]),
        body=body,
        tags=tags,
        metadata=metadata,
    )


def _markdown_to_record(
    path: Path,
    record_type: str,
    source_name: str = "codex",
    extra_tags: list[str] | None = None,
) -> MemoryRecord | None:
    """Read a markdown file (skill or memory) as a single record."""
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

    title_raw = meta.get("name") or meta.get("title")
    if isinstance(title_raw, str) and title_raw.strip():
        title = title_raw.strip()
    else:
        title = _extract_h1(body) or path.parent.name or path.stem

    raw_tags = meta.get("tags", [])
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    if extra_tags:
        tags = [*tags, *extra_tags]

    mtime_iso = _file_mtime_iso(path)
    created = meta.get("created") or mtime_iso
    updated = meta.get("updated") or mtime_iso

    return MemoryRecord(
        source=source_name,
        ref=str(path),
        title=title,
        body=body,
        tags=tags,
        metadata={
            "type": record_type,
            "created": str(created),
            "updated": str(updated),
            "raw_frontmatter": meta,
        },
    )


def _is_user_skill_path(skill_md: Path, skills_root: Path) -> bool:
    """True if ``skill_md`` is a user-authored skill, not a vendor skill.

    Excludes anything under a dotfile directory (``.system/`` and any
    future hidden subdirs). See ADR 0022 for why ``.system/`` is hard-
    excluded rather than tagged.
    """
    try:
        rel = skill_md.relative_to(skills_root)
    except ValueError:
        return False
    return not any(part.startswith(".") for part in rel.parts)


def _iter_session_files(root: Path) -> Iterator[Path]:
    if not root.exists() or not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_file():
            yield path


def _iter_skill_files(root: Path) -> Iterator[Path]:
    if not root.exists() or not root.is_dir():
        return
    for path in sorted(root.rglob("SKILL.md")):
        if path.is_file() and _is_user_skill_path(path, root):
            yield path


def _iter_memory_files(root: Path) -> Iterator[Path]:
    if not root.exists() or not root.is_dir():
        return
    for path in sorted(root.glob("*.md")):
        if path.is_file():
            yield path


class _EventHandler(FileSystemEventHandler):
    """Marshals filesystem events into an asyncio queue with the
    matching root, so the consumer can dispatch on file type."""

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
                "MEMSTEM_CODEX_WATCH_DEBOUNCE_SECONDS",
                str(self.DEFAULT_DEBOUNCE_SECONDS),
            )
        )

    def _enqueue(self, src: str) -> None:
        path = Path(src)
        if path.suffix not in (".jsonl", ".md"):
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
        dest = getattr(event, "dest_path", None)
        if not event.is_directory and dest:
            self._enqueue(str(dest))


class CodexAdapter(Adapter):
    """Reads Codex sessions, user skills, and memories from disk."""

    name = "codex"

    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        skills_root: Path | None = None,
        memories_root: Path | None = None,
    ) -> None:
        self.sessions_root = Path(sessions_root).expanduser().resolve() if sessions_root else None
        self.skills_root = Path(skills_root).expanduser().resolve() if skills_root else None
        self.memories_root = Path(memories_root).expanduser().resolve() if memories_root else None

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        # `paths` is accepted for ABC compatibility but ignored — the
        # adapter discovers its roots from constructor arguments. The
        # daemon passes an empty list when constructing this adapter.
        del paths

        if self.sessions_root is not None:
            for path in _iter_session_files(self.sessions_root):
                record = _session_to_record(path, self.name)
                if record is not None:
                    yield record

        if self.skills_root is not None:
            for path in _iter_skill_files(self.skills_root):
                record = _markdown_to_record(path, "skill", self.name)
                if record is not None:
                    yield record

        if self.memories_root is not None:
            for path in _iter_memory_files(self.memories_root):
                record = _markdown_to_record(path, "memory", self.name)
                if record is not None:
                    yield record

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        del paths

        queue: asyncio.Queue[Path] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        observer = Observer()
        handler = _EventHandler(loop=loop, queue=queue)

        roots: list[Path] = []
        for root in (self.sessions_root, self.skills_root, self.memories_root):
            if root is not None and root.exists():
                observer.schedule(handler, str(root), recursive=True)
                roots.append(root)

        if not roots:
            # Nothing to watch — keep the coroutine alive so the daemon
            # task list stays valid, but never yield.
            await asyncio.Event().wait()
            return

        observer.start()
        try:
            while True:
                changed = await queue.get()
                if not changed.exists() or not changed.is_file():
                    continue
                resolved = changed.resolve()

                record = self._dispatch(resolved)
                if record is not None:
                    yield record
        finally:
            observer.stop()
            observer.join()

    def _dispatch(self, path: Path) -> MemoryRecord | None:
        """Pick the right parser based on which root the path lives under."""
        if (
            self.sessions_root is not None
            and _is_under(path, self.sessions_root)
            and path.suffix == ".jsonl"
        ):
            return _session_to_record(path, self.name)

        if (
            self.skills_root is not None
            and _is_under(path, self.skills_root)
            and path.name == "SKILL.md"
            and _is_user_skill_path(path, self.skills_root)
        ):
            return _markdown_to_record(path, "skill", self.name)

        if (
            self.memories_root is not None
            and path.parent == self.memories_root
            and path.suffix == ".md"
        ):
            return _markdown_to_record(path, "memory", self.name)

        return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["CodexAdapter"]
