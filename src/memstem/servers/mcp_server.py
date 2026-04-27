"""MCP server exposing Memstem search, get, list_skills, get_skill, upsert.

Built on `FastMCP` from the official `mcp` Python SDK. Tools match the
contract in `docs/mcp-api.md`. The server is a pure factory — the daemon
(or test harness) constructs `Vault`, `Index`, and an optional embedder
and passes them to `build_server()`.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from memstem.config import SearchConfig
from memstem.core.embeddings import Embedder
from memstem.core.frontmatter import Frontmatter, MemoryType, validate
from memstem.core.index import Index
from memstem.core.search import Result, Search
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 240


class _ActivityTracker:
    """Shared, thread-safe last-activity timestamp for the idle watcher.

    Tools record activity by calling ``touch()`` at the start of every
    request handler. The watcher thread reads ``idle_seconds()``
    periodically and triggers process exit when the elapsed idle
    interval crosses the configured threshold.
    """

    __slots__ = ("_last", "_lock")

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last


def _start_idle_watcher(
    activity: _ActivityTracker,
    idle_timeout_seconds: int,
    *,
    check_interval_seconds: float | None = None,
    exit_fn: Callable[[], None] | None = None,
) -> threading.Thread:
    """Start the daemon thread that exits the process after idle timeout.

    The watcher polls ``activity.idle_seconds()`` every
    ``check_interval_seconds`` seconds. When idle is at least
    ``idle_timeout_seconds`` it calls ``exit_fn``, which by default
    sends SIGTERM to the current process so FastMCP's signal handler
    can shut down cleanly. ``exit_fn`` is parameterised so tests can
    swap in a sentinel without actually killing pytest.
    """
    if check_interval_seconds is None:
        # Poll every 1/10th of the timeout, clamped to [5s, 60s]. Picking
        # 10% means the worst-case overshoot before we notice idleness
        # is one decile of the threshold — fine for our 30-minute default.
        check_interval_seconds = max(5.0, min(60.0, idle_timeout_seconds / 10))

    if exit_fn is None:

        def _default_exit() -> None:
            logger.info(
                "MCP server idle for >= %ds; exiting (SIGTERM to self)",
                idle_timeout_seconds,
            )
            os.kill(os.getpid(), signal.SIGTERM)

        exit_fn = _default_exit

    def _watch() -> None:
        # Capture as a local for type narrowing inside the loop.
        do_exit = exit_fn
        assert do_exit is not None
        while True:
            time.sleep(check_interval_seconds)
            if activity.idle_seconds() >= idle_timeout_seconds:
                do_exit()
                return

    thread = threading.Thread(target=_watch, name="mcp-idle-watcher", daemon=True)
    thread.start()
    return thread


def _snippet(body: str, length: int = SNIPPET_CHARS) -> str:
    text = " ".join(body.split())
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "…"


def _serialize_memory(memory: Memory) -> dict[str, Any]:
    return {
        "id": str(memory.id),
        "type": memory.type.value,
        "title": memory.frontmatter.title,
        "body": memory.body,
        "path": str(memory.path),
        "frontmatter": memory.frontmatter.model_dump(mode="json", exclude_none=True),
    }


def _serialize_result(result: Result) -> dict[str, Any]:
    memory = result.memory
    return {
        "id": str(memory.id),
        "title": memory.frontmatter.title,
        "type": memory.type.value,
        "snippet": _snippet(memory.body),
        "score": result.score,
        "path": str(memory.path),
        "frontmatter": memory.frontmatter.model_dump(mode="json", exclude_none=True),
        "bm25_rank": result.bm25_rank,
        "vec_rank": result.vec_rank,
    }


def _auto_path(fm: Frontmatter) -> Path:
    """Compute a vault-relative path from frontmatter type + id."""
    if fm.type is MemoryType.SKILL:
        slug = (fm.title or str(fm.id))[:64].lower().replace(" ", "-")
        return Path(f"skills/{slug}.md")
    if fm.type is MemoryType.DAILY:
        return Path(f"daily/{fm.created.date().isoformat()}.md")
    if fm.type is MemoryType.SESSION:
        return Path(f"sessions/{fm.id}.md")
    return Path(f"memories/{fm.id}.md")


def build_server(
    vault: Vault,
    index: Index,
    embedder: Embedder | None = None,
    *,
    name: str = "memstem",
    search_config: SearchConfig | None = None,
    idle_timeout_seconds: int = 0,
) -> FastMCP:
    """Construct a FastMCP server bound to the given vault/index/embedder.

    ``search_config`` lets the caller thread the user's configured RRF
    parameters (``rrf_k``, ``bm25_weight``, ``vector_weight``) through
    to every ``memstem_search`` call. Defaults to ``SearchConfig()``
    when omitted, which matches the legacy 60/1.0/1.0 behavior.

    ``idle_timeout_seconds`` enables MCP-process self-termination after
    the configured idle period. Each Claude Code session spawns its
    own ``memstem mcp`` subprocess; without this, those subprocesses
    linger after the parent session ends and accumulate over weeks
    until they contend on the SQLite write lock. ``0`` (the default
    for the factory) disables auto-exit; the CLI's ``memstem mcp``
    command threads ``cfg.mcp.idle_timeout_seconds`` here.
    """
    mcp = FastMCP(name)
    search = Search(vault=vault, index=index, embedder=embedder)
    sc = search_config or SearchConfig()
    activity = _ActivityTracker()
    if idle_timeout_seconds > 0:
        _start_idle_watcher(activity, idle_timeout_seconds)

    @mcp.tool()
    async def memstem_search(
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid keyword + semantic search across memories and skills."""
        activity.touch()
        results = search.search(
            query=query,
            limit=limit,
            types=types,
            rrf_k=sc.rrf_k,
            bm25_weight=sc.bm25_weight,
            vector_weight=sc.vector_weight,
        )
        return [_serialize_result(r) for r in results]

    @mcp.tool()
    async def memstem_get(id_or_path: str) -> dict[str, Any]:
        """Retrieve a single memory by vault-relative path or by id."""
        activity.touch()
        # Try path first (most common); fall back to id lookup via the index.
        try:
            memory = vault.read(id_or_path)
            return _serialize_memory(memory)
        except MemoryNotFoundError:
            pass
        row = index.db.execute("SELECT path FROM memories WHERE id = ?", (id_or_path,)).fetchone()
        if row is None:
            raise ValueError(f"no memory found for {id_or_path!r}")
        memory = vault.read(row["path"])
        return _serialize_memory(memory)

    @mcp.tool()
    async def memstem_list_skills(
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available skills, optionally filtered by scope."""
        activity.touch()
        rows = index.db.execute(
            "SELECT id, path FROM memories WHERE type = ?", ("skill",)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                memory = vault.read(row["path"])
            except MemoryNotFoundError:
                continue
            fm = memory.frontmatter
            if scope is not None and fm.scope != scope:
                continue
            out.append(
                {
                    "id": str(memory.id),
                    "title": fm.title,
                    "scope": fm.scope,
                    "prerequisites": list(fm.prerequisites),
                }
            )
        return out

    @mcp.tool()
    async def memstem_get_skill(name: str) -> dict[str, Any]:
        """Retrieve a skill by exact title match."""
        activity.touch()
        rows = index.db.execute(
            "SELECT path FROM memories WHERE type = ? AND title = ?",
            ("skill", name),
        ).fetchall()
        if not rows:
            raise ValueError(f"no skill named {name!r}")
        return _serialize_memory(vault.read(rows[0]["path"]))

    @mcp.tool()
    async def memstem_upsert(
        frontmatter: dict[str, Any],
        body: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Add or update a memory.

        Validates `frontmatter` against the canonical schema. If `path` is
        omitted, generates a vault-relative path from type + id.
        """
        activity.touch()
        fm = validate(frontmatter)
        target_path = Path(path) if path else _auto_path(fm)
        memory = Memory(frontmatter=fm, body=body, path=target_path)
        vault.write(memory)
        index.upsert(memory)
        return {
            "id": str(fm.id),
            "path": str(target_path),
            "created": fm.created.isoformat(),
        }

    return mcp


__all__ = ["build_server"]
