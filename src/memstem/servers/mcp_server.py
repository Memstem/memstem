"""MCP server exposing Memstem search, get, list_skills, get_skill, upsert.

Built on `FastMCP` from the official `mcp` Python SDK. Tools match the
contract in `docs/mcp-api.md`. The server is a pure factory — the daemon
(or test harness) constructs `Vault`, `Index`, and an optional embedder
and passes them to `build_server()`, either as already-initialized
objects or as factory callables that build them on first tool use.

Lazy resource initialization keeps the MCP handshake (``initialize`` +
``tools/list``) fast even when the underlying index is large. MCP clients
typically apply a connection timeout (OpenClaw's bundle-mcp defaults to
30s) that fires during the handshake; opening a multi-hundred-MB SQLite
+ ``sqlite-vec`` index synchronously at server start can blow past it.
With lazy init, the handshake completes in milliseconds and the load
cost lands on the first ``memstem_search`` (or other tool) call instead.
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


class _Resources:
    """Holds the heavy resources (vault, index, embedder, search) for the MCP server.

    Construct with :meth:`eager` when you already have initialized objects
    (test harness, daemon embed) or with :meth:`lazy` when you want to
    defer the load until the first tool call (the production CLI path).

    Access is thread-safe: a lock guards each lazy initialization so
    concurrent tool calls during the first-call window don't double-init.
    Initialization is one-shot — once a property has been resolved, it's
    cached for the lifetime of the holder.
    """

    __slots__ = (
        "_build_embedder",
        "_build_index",
        "_build_vault",
        "_embedder",
        "_embedder_resolved",
        "_index",
        "_lock",
        "_search",
        "_vault",
    )

    def __init__(
        self,
        *,
        build_vault: Callable[[], Vault],
        build_index: Callable[[], Index],
        build_embedder: Callable[[], Embedder | None],
    ) -> None:
        self._build_vault = build_vault
        self._build_index = build_index
        self._build_embedder = build_embedder
        self._vault: Vault | None = None
        self._index: Index | None = None
        self._embedder: Embedder | None = None
        self._embedder_resolved = False
        self._search: Search | None = None
        # Use an RLock because `search` composes the cached vault/index/embedder
        # via their properties while holding the same guard. A plain Lock would
        # self-deadlock on first `res.search` access in lazy mode.
        self._lock = threading.RLock()

    @classmethod
    def eager(
        cls,
        vault: Vault,
        index: Index,
        embedder: Embedder | None = None,
    ) -> _Resources:
        """Wrap already-initialized resources. Used by tests and any caller
        that constructs the heavy objects itself.
        """
        instance = cls(
            build_vault=lambda: vault,
            build_index=lambda: index,
            build_embedder=lambda: embedder,
        )
        # Pre-resolve so callers that own the index lifecycle externally
        # (test fixtures, daemon embed) don't see surprise factory invocations.
        instance._vault = vault
        instance._index = index
        instance._embedder = embedder
        instance._embedder_resolved = True
        return instance

    @classmethod
    def lazy(
        cls,
        *,
        build_vault: Callable[[], Vault],
        build_index: Callable[[], Index],
        build_embedder: Callable[[], Embedder | None],
    ) -> _Resources:
        """Defer construction until first access. The CLI's ``mcp`` command
        uses this so the MCP handshake is fast even with a multi-hundred-MB
        on-disk index.
        """
        return cls(
            build_vault=build_vault,
            build_index=build_index,
            build_embedder=build_embedder,
        )

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            with self._lock:
                if self._vault is None:
                    self._vault = self._build_vault()
        return self._vault

    @property
    def index(self) -> Index:
        if self._index is None:
            with self._lock:
                if self._index is None:
                    self._index = self._build_index()
        return self._index

    @property
    def embedder(self) -> Embedder | None:
        if not self._embedder_resolved:
            with self._lock:
                if not self._embedder_resolved:
                    self._embedder = self._build_embedder()
                    self._embedder_resolved = True
        return self._embedder

    @property
    def search(self) -> Search:
        if self._search is None:
            with self._lock:
                if self._search is None:
                    self._search = Search(
                        vault=self.vault,
                        index=self.index,
                        embedder=self.embedder,
                    )
        return self._search

    @property
    def index_initialized(self) -> bool:
        """True iff the index factory has been invoked. Lets callers (e.g.
        the CLI) close the index only when it was actually opened."""
        return self._index is not None


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
    vault: Vault | None = None,
    index: Index | None = None,
    embedder: Embedder | None = None,
    *,
    resources: _Resources | None = None,
    name: str = "memstem",
    search_config: SearchConfig | None = None,
    idle_timeout_seconds: int = 0,
) -> FastMCP:
    """Construct a FastMCP server bound to the given resources.

    Two construction paths:

    - **Eager (backward-compatible):** pass ``vault`` and ``index`` (and
      optionally ``embedder``). Resources are already loaded; tool calls
      use them directly. This is what test fixtures and the daemon's
      in-process embed do.
    - **Lazy:** pass ``resources`` constructed via
      :meth:`_Resources.lazy`, leaving ``vault`` and ``index`` ``None``.
      Tool calls resolve resources on first access; the MCP handshake
      does not touch them. Used by the ``memstem mcp`` CLI command so
      OpenClaw's 30-second connection timeout doesn't fire while the
      server is still opening a large index.

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
    if resources is not None:
        if vault is not None or index is not None or embedder is not None:
            raise ValueError(
                "build_server: pass either resources= or (vault, index, embedder), not both"
            )
        res = resources
    else:
        if vault is None or index is None:
            raise ValueError("build_server: provide vault and index, or resources= for lazy init")
        res = _Resources.eager(vault, index, embedder)

    mcp = FastMCP(name)
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
        results = res.search.search(
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
            memory = res.vault.read(id_or_path)
            return _serialize_memory(memory)
        except MemoryNotFoundError:
            pass
        row = res.index.db.execute(
            "SELECT path FROM memories WHERE id = ?", (id_or_path,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no memory found for {id_or_path!r}")
        memory = res.vault.read(row["path"])
        return _serialize_memory(memory)

    @mcp.tool()
    async def memstem_list_skills(
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available skills, optionally filtered by scope."""
        activity.touch()
        rows = res.index.db.execute(
            "SELECT id, path FROM memories WHERE type = ?", ("skill",)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                memory = res.vault.read(row["path"])
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
        rows = res.index.db.execute(
            "SELECT path FROM memories WHERE type = ? AND title = ?",
            ("skill", name),
        ).fetchall()
        if not rows:
            raise ValueError(f"no skill named {name!r}")
        return _serialize_memory(res.vault.read(rows[0]["path"]))

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
        res.vault.write(memory)
        res.index.upsert(memory)
        return {
            "id": str(fm.id),
            "path": str(target_path),
            "created": fm.created.isoformat(),
        }

    return mcp


__all__ = ["build_server"]
