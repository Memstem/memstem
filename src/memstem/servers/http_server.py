"""Local HTTP API exposing Memstem search and read endpoints.

Co-hosted in `memstem daemon` so first-party clients (the Obsidian
plugin, future VS Code/web clients) can call into the same `Search` /
`Vault` / `Index` instances the watch loop and embed worker already use,
without spawning a `memstem search` subprocess per query.

Loopback-only by design. No auth in v0.1 — the design assumption is
that anything able to reach `127.0.0.1:7821` already has filesystem
access to the vault. Binding to a non-loopback address is allowed by
config but explicitly logged as a footgun on startup.

Endpoint shapes mirror the MCP tool list one-to-one so the two surfaces
stay in lockstep:

| HTTP                        | MCP tool             |
|-----------------------------|----------------------|
| `GET  /health`              | (server-only)        |
| `GET  /version`             | (server-only)        |
| `POST /search`              | `memstem_search`     |
| `GET  /memory/{id_or_path}` | `memstem_get`        |

Future endpoints (`POST /upsert`, `GET /skills`, websocket for live
ingestion events) are deliberately deferred until a plugin feature
needs them — adding to a stable surface is easy; removing isn't.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import memstem
from memstem.config import HttpServerConfig, SearchConfig
from memstem.core.embeddings import Embedder
from memstem.core.index import Index
from memstem.core.search import Result, Search
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 240


class SearchRequest(BaseModel):
    """Body of `POST /search`. All fields except ``query`` are optional;
    omitted fields fall back to ``SearchConfig`` defaults."""

    query: str
    limit: int = 10
    types: list[str] | None = None
    rrf_k: int | None = None
    bm25_weight: float | None = None
    vector_weight: float | None = None


class SearchHit(BaseModel):
    """One search result, mirrors the MCP `_serialize_result` shape."""

    id: str
    title: str | None
    type: str
    snippet: str
    score: float
    path: str
    bm25_rank: int | None
    vec_rank: int | None
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class MemoryDetail(BaseModel):
    """Full memory body, mirrors the MCP `_serialize_memory` shape."""

    id: str
    type: str
    title: str | None
    body: str
    path: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)


def _snippet(body: str, length: int = SNIPPET_CHARS) -> str:
    text = " ".join(body.split())
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "…"


def _serialize_result(result: Result) -> SearchHit:
    memory = result.memory
    return SearchHit(
        id=str(memory.id),
        title=memory.frontmatter.title,
        type=memory.type.value,
        snippet=_snippet(memory.body),
        score=result.score,
        path=str(memory.path),
        bm25_rank=result.bm25_rank,
        vec_rank=result.vec_rank,
        frontmatter=memory.frontmatter.model_dump(mode="json", exclude_none=True),
    )


def _serialize_memory(memory: Memory) -> MemoryDetail:
    return MemoryDetail(
        id=str(memory.id),
        type=memory.type.value,
        title=memory.frontmatter.title,
        body=memory.body,
        path=str(memory.path),
        frontmatter=memory.frontmatter.model_dump(mode="json", exclude_none=True),
    )


def build_app(
    vault: Vault,
    index: Index,
    embedder: Embedder | None = None,
    *,
    search_config: SearchConfig | None = None,
) -> FastAPI:
    """Construct the FastAPI app bound to the given vault/index/embedder.

    ``search_config`` defaults to ``SearchConfig()``; supply the loaded
    config so the daemon's RRF tuning is the same on both the MCP and
    HTTP surfaces.
    """
    app = FastAPI(
        title="Memstem",
        version=memstem.__version__,
        description="Local HTTP API over Memstem's vault + index.",
    )

    # CORS so an Obsidian plugin (which runs in a renderer) can call us.
    # Loopback-only so this is safe; plugin requests look like
    # cross-origin to the browser even on localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["app://obsidian.md", "capacitor://localhost", "http://localhost"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    search = Search(vault=vault, index=index, embedder=embedder)
    sc = search_config or SearchConfig()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness probe + version. Used by clients to discover the daemon."""
        return {
            "status": "ok",
            "version": memstem.__version__,
            "vault": str(vault.root),
            "embedder": embedder is not None,
        }

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": memstem.__version__}

    @app.post("/search", response_model=list[SearchHit])
    async def do_search(req: SearchRequest) -> list[SearchHit]:
        """Hybrid keyword + semantic search across memories and skills."""
        results = search.search(
            query=req.query,
            limit=req.limit,
            types=req.types,
            rrf_k=req.rrf_k if req.rrf_k is not None else sc.rrf_k,
            bm25_weight=(req.bm25_weight if req.bm25_weight is not None else sc.bm25_weight),
            vector_weight=(
                req.vector_weight if req.vector_weight is not None else sc.vector_weight
            ),
        )
        return [_serialize_result(r) for r in results]

    @app.get("/memory/{id_or_path:path}", response_model=MemoryDetail)
    async def get_memory(id_or_path: str) -> MemoryDetail:
        """Retrieve a single memory by vault-relative path or by id."""
        try:
            memory = vault.read(id_or_path)
            return _serialize_memory(memory)
        except MemoryNotFoundError:
            pass
        row = index.db.execute("SELECT path FROM memories WHERE id = ?", (id_or_path,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no memory for {id_or_path!r}")
        memory = vault.read(row["path"])
        return _serialize_memory(memory)

    return app


async def serve(
    config: HttpServerConfig,
    vault: Vault,
    index: Index,
    embedder: Embedder | None = None,
    *,
    search_config: SearchConfig | None = None,
) -> None:
    """Run the HTTP server forever. Cancel-safe.

    Designed to be one of the tasks in `asyncio.gather(...)` inside
    `_run_daemon` — when the daemon is cancelled, uvicorn exits via the
    `should_exit` flag set by its lifespan handlers.
    """
    if not config.enabled:
        logger.info("http server disabled in config; skipping")
        return

    if config.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "http server bound to non-loopback address %s — there is no auth in v0.1; "
            "anyone reachable can read your vault",
            config.host,
        )

    # uvicorn is imported lazily so users running `memstem search` (which
    # never starts the server) don't pay the import cost.
    import uvicorn

    app = build_app(vault, index, embedder, search_config=search_config)
    server_config = uvicorn.Config(
        app=app,
        host=config.host,
        port=config.port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(server_config)
    logger.info("http server listening on %s:%s", config.host, config.port)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


__all__ = ["MemoryDetail", "SearchHit", "SearchRequest", "build_app", "serve"]
