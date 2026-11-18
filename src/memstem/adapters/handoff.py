"""Handoff notes: one searchable record per note, found by polling project roots (ADR 0057).

Agents leave a handoff note (``HANDOFF.md``, ``HANDOFF-2026-11-13-topic.md``) in the
project they worked in. Project trees are far too large for a recursive watch, and git
worktrees hold many copies of the same note, so this adapter walks the configured roots
on a timer, groups copies into notes, and emits the most recently written copy of each
under a ``ref`` that does not change when the winning copy does.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.config import HandoffRoot
from memstem.core.frontmatter import load_post
from memstem.core.skipped import SKIPPED

logger = logging.getLogger(__name__)

SOURCE = "handoff"
PRUNED_DIRS = frozenset(
    {"node_modules", "site-packages", "venv", "__pycache__", "dist", "build", "target", "vendor"}
)
_WORKTREE_MARKER = f"{os.sep}.git{os.sep}worktrees{os.sep}"
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_GIT = shutil.which("git")
_GIT_TIMEOUT_S = 10


@dataclass(frozen=True)
class Note:
    """The winning copy of one handoff note."""

    ref: str
    """Identity path: stable across worktree copies; need not exist on disk."""
    path: Path
    """The most recently written copy, the one that is read."""
    project: str
    written_ns: int
    """When this copy's content was written (see :func:`_committed_ns`)."""
    mtime_ns: int
    size: int

    @property
    def fingerprint(self) -> tuple[str, int, int]:
        return (str(self.path), self.mtime_ns, self.size)


def _repo_for(
    directory: str, cache: dict[str, tuple[str, str] | None], stop: str | None = None
) -> tuple[str, str] | None:
    """``(main working tree, this working tree)`` for the repository holding ``directory``.

    A linked worktree's ``.git`` is a file naming ``<main>/.git/worktrees/<name>``; that
    is read directly, no ``git`` process is run. Any other ``.git`` (a directory, a
    submodule's file) marks a repository that is its own main working tree. The search
    goes no higher than ``stop`` (the filesystem root when ``None``).
    """
    trail: list[str] = []
    current = directory
    found: tuple[str, str] | None = None
    while True:
        if current in cache:
            found = cache[current]
            break
        trail.append(current)
        dot_git = os.path.join(current, ".git")
        if os.path.isdir(dot_git):
            found = (current, current)
            break
        if os.path.isfile(dot_git):
            found = (current, current)
            try:
                with open(dot_git, encoding="utf-8", errors="replace") as fh:
                    pointer = fh.read(4096).strip()
            except OSError:
                pointer = ""
            if pointer.startswith("gitdir:"):
                gitdir = pointer.split(":", 1)[1].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(current, gitdir))
                if _WORKTREE_MARKER in gitdir:
                    found = (gitdir.split(_WORKTREE_MARKER, 1)[0], current)
            break
        parent = os.path.dirname(current)
        if parent == current or current == stop:
            break
        current = parent
    for seen in trail:
        cache[seen] = found
    return found


def _git(tree: str, *args: str) -> str | None:
    if _GIT is None:
        return None
    try:
        done = subprocess.run(
            [_GIT, "-C", tree, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            # Never take the index lock: other sessions are working in these trees.
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _committed_ns(tree: str, rel: str, full: str) -> int | None:
    """Commit time of a file whose working copy is exactly what ``HEAD`` holds.

    A checkout stamps every file with the time of the checkout, so in a working tree
    the modification time says when the tree was made, not when the note was written:
    a new worktree of an old branch would look newer than the note someone edited
    yesterday. For an unmodified tracked file the last commit that touched it is the
    truth. ``None`` (untracked, edited since the commit, not a repository, no git)
    means the modification time is the best evidence there is.
    """
    blob = _git(tree, "rev-parse", "--verify", "--quiet", f"HEAD:{rel}")
    if not blob:
        return None
    try:
        data = Path(full).read_bytes()
    except OSError:
        return None
    header = b"blob %d\0" % len(data)
    if hashlib.sha1(header + data, usedforsecurity=False).hexdigest() != blob:
        return None
    stamp = _git(tree, "log", "-1", "--format=%ct", "HEAD", "--", rel)
    return int(stamp) * 10**9 if stamp and stamp.isdigit() else None


def _filename_date(name: str) -> date | None:
    for match in _FILENAME_DATE_RE.finditer(name):
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
    return None


def _note_to_record(note: Note) -> MemoryRecord | None:
    try:
        text = note.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not read handoff note %s: %s", note.path, exc)
        SKIPPED.record(note.path, exc, source=SOURCE, kind="unreadable")
        return None
    try:
        post, _recovered = load_post(text)
    except Exception as exc:
        logger.warning("frontmatter parse failed for %s: %s", note.path, exc)
        SKIPPED.record(note.path, exc, source=SOURCE)
        return None
    SKIPPED.clear(note.path)

    content = post.content.strip()
    if not content:
        return None
    meta = dict(post.metadata)

    modified = datetime.fromtimestamp(note.written_ns / 1e9, tz=UTC)
    named = _filename_date(note.path.name)
    # A note with no date in its name is dated the day it was last written, as the
    # operator would read it off a calendar: local time, not UTC.
    day = named or datetime.fromtimestamp(note.written_ns / 1e9).date()
    created = datetime(named.year, named.month, named.day, tzinfo=UTC) if named else modified

    label = meta.get("title")
    if not isinstance(label, str) or not label.strip():
        heading = _H1_RE.search(content)
        label = heading.group(1) if heading else note.path.stem
    title = f"{note.project} handoff {day.isoformat()}: {label.strip()[:120]}"

    raw_tags = meta.get("tags", [])
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    for tag in ("handoff", f"project:{note.project}"):
        if tag not in tags:
            tags.append(tag)

    # Vectors are built from the body alone, so the project and date must be in it.
    header = f"Handoff note for project {note.project}, dated {day.isoformat()}. Source: {note.ref}"
    return MemoryRecord(
        source=SOURCE,
        ref=note.ref,
        title=title,
        body=f"{header}\n\n{content}",
        tags=tags,
        metadata={
            "type": "memory",
            "created": created.isoformat(),
            "updated": modified.isoformat(),
            "raw_frontmatter": meta,
        },
    )


class HandoffAdapter(Adapter):
    name = SOURCE

    def __init__(
        self,
        *,
        roots: list[HandoffRoot],
        patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        exclude_dirs: list[str] | None = None,
        max_depth: int = 8,
        max_file_bytes: int = 2 * 1024 * 1024,
        poll_seconds: float = 300,
    ) -> None:
        self.roots = [
            HandoffRoot(path=Path(os.path.abspath(r.path.expanduser())), newest_only=r.newest_only)
            for r in roots
        ]
        self.patterns = [p.lower() for p in (patterns or ["*handoff*.md"])]
        self.exclude_patterns = [p.lower() for p in (exclude_patterns or [])]
        self.pruned = PRUNED_DIRS | frozenset(exclude_dirs or [])
        self.max_depth = max_depth
        self.max_file_bytes = max_file_bytes
        self.poll_seconds = poll_seconds
        self._notes: dict[str, Note] | None = None
        self._emitted: dict[str, tuple[str, int, int]] = {}
        self._written: dict[tuple[str, int, int], int] = {}
        self._watching = False
        self._healthy = True

    def source_roots(self) -> list[Path]:
        return [r.path for r in self.roots]

    def source_exists(self, ref: str) -> bool:
        """A note exists while any copy of it does (the ref itself may not be a file)."""
        if self._notes is None:
            self._notes = self.scan()
        return ref in self._notes

    def watcher_alive(self) -> bool | None:
        return self._healthy if self._watching else None

    def _is_note(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatchcase(low, p) for p in self.patterns) and not any(
            fnmatchcase(low, p) for p in self.exclude_patterns
        )

    def scan(self) -> dict[str, Note]:
        """Walk every root and return the winning copy of each note, keyed by ref."""
        seen: set[str] = set()
        notes: dict[str, Note] = {}
        written_cache: dict[tuple[str, int, int], int] = {}
        for root in self.roots:
            base = str(root.path)
            base_depth = base.rstrip(os.sep).count(os.sep)
            # Copies are grouped by repositories found at or below the root. A
            # repository that merely contains the root (the root is one directory of a
            # working tree, or the home directory is a dotfiles repository) only names
            # the notes that have no directory of their own below the root.
            repos: dict[str, tuple[str, str] | None] = {}
            outer = _repo_for(base, {})
            root_project = os.path.basename(outer[0]) if outer else root.path.name
            for dirpath, dirnames, filenames in os.walk(base):
                if dirpath.count(os.sep) - base_depth >= self.max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        d for d in dirnames if d not in self.pruned and not d.startswith(".")
                    ]
                for name in filenames:
                    if not self._is_note(name):
                        continue
                    full = os.path.join(dirpath, name)
                    if full in seen:
                        continue  # roots may overlap
                    seen.add(full)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    if not os.path.isfile(full) or st.st_size > self.max_file_bytes:
                        continue
                    repo = None if root.newest_only else _repo_for(dirpath, repos, base)
                    if root.newest_only:
                        ref = os.path.join(base, name)
                        project = root_project
                    elif repo is not None:
                        main, tree = repo
                        ref = os.path.join(main, os.path.relpath(full, tree))
                        project = os.path.basename(main)
                    else:
                        ref = full
                        below = os.path.relpath(full, base).split(os.sep)
                        project = below[0] if len(below) > 1 else root_project
                    # Git is asked once per version of a file, not once per scan.
                    key = (full, st.st_mtime_ns, st.st_size)
                    written = self._written.get(key)
                    if written is None:
                        committed = (
                            _committed_ns(repo[1], os.path.relpath(full, repo[1]), full)
                            if repo is not None
                            else None
                        )
                        written = committed if committed is not None else st.st_mtime_ns
                    written_cache[key] = written
                    held = notes.get(ref)
                    # Newest wins; on a tie the copy at the identity path, then the
                    # lowest path, so the choice never depends on walk order.
                    rank = (-written, full != ref, full)
                    if held is None or rank < (
                        -held.written_ns,
                        str(held.path) != ref,
                        str(held.path),
                    ):
                        notes[ref] = Note(
                            ref, Path(full), project, written, st.st_mtime_ns, st.st_size
                        )
        self._written = written_cache
        return notes

    def _collect(self, *, changed_only: bool) -> list[MemoryRecord]:
        notes = self.scan()
        self._notes = notes
        records: list[MemoryRecord] = []
        for ref, note in notes.items():
            if changed_only and self._emitted.get(ref) == note.fingerprint:
                continue
            record = _note_to_record(note)
            self._emitted[ref] = note.fingerprint
            if record is not None:
                records.append(record)
        for ref in self._emitted.keys() - notes.keys():
            del self._emitted[ref]
        return records

    async def _emit(self, *, changed_only: bool) -> AsyncGenerator[MemoryRecord, None]:
        try:
            records = await asyncio.to_thread(self._collect, changed_only=changed_only)
            self._healthy = True
        except OSError:
            self._healthy = False
            logger.exception("handoff note scan failed; will retry")
            return
        for record in records:
            yield record

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        async for record in self._emit(changed_only=False):
            yield record

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        self._watching = True
        try:
            while True:
                async for record in self._emit(changed_only=True):
                    yield record
                await asyncio.sleep(self.poll_seconds)
        finally:
            self._watching = False
