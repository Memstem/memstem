"""MCP server exposing Memstem search, get, list_skills, get_skill, upsert.

Built on `FastMCP` from the official `mcp` Python SDK. Tools match the
contract in `docs/mcp-api.md`. The server is a pure factory — the daemon
(or test harness) constructs `Vault`, `Index`, and an optional embedder
and passes them to `build_server()`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from memstem.core.embeddings import Embedder
from memstem.core.frontmatter import Frontmatter, MemoryType, validate
from memstem.core.index import Index
from memstem.core.search import Result, Search
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 240


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
) -> FastMCP:
    """Construct a FastMCP server bound to the given vault/index/embedder."""
    mcp = FastMCP(name)
    search = Search(vault=vault, index=index, embedder=embedder)

    @mcp.tool()
    async def memstem_search(
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid keyword + semantic search across memories and skills."""
        results = search.search(query=query, limit=limit, types=types)
        return [_serialize_result(r) for r in results]

    @mcp.tool()
    async def memstem_get(id_or_path: str) -> dict[str, Any]:
        """Retrieve a single memory by vault-relative path or by id."""
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
