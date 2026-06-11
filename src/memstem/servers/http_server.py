"""Local HTTP API exposing Memstem search and read endpoints.

Co-hosted in `memstem daemon` so first-party clients (CLI tools, future
editor extensions, internal automation) can call into the same `Search`
/ `Vault` / `Index` instances the watch loop and embed worker already
use, without spawning a `memstem search` subprocess per query.

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
ingestion events) are deliberately deferred until a client feature
needs them — adding to a stable surface is easy; removing isn't.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import memstem
from memstem.adapters.base import Adapter
from memstem.config import HttpServerConfig, HygieneConfig, SearchConfig
from memstem.core.embeddings import Embedder
from memstem.core.index import Index
from memstem.core.rerank import build_reranker, effective_rerank_top_n
from memstem.core.retrieval_log import log_get
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
    importance_weight: float | None = None
    type_bias: dict[str, float] | None = None
    mmr_lambda: float | None = None
    rerank_top_n: int | None = None


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
    hygiene_config: HygieneConfig | None = None,
    adapters: Sequence[Adapter] | None = None,
) -> FastAPI:
    """Construct the FastAPI app bound to the given vault/index/embedder.

    ``search_config`` defaults to ``SearchConfig()``; supply the loaded
    config so the daemon's RRF tuning is the same on both the MCP and
    HTTP surfaces.

    ``hygiene_config`` defaults to ``HygieneConfig()``; threading the
    loaded config keeps the ADR 0008 query-log enabled/disabled state
    consistent with the daemon-side hygiene worker.

    ``adapters`` lets ``/health`` report per-adapter watcher-thread
    liveness (B4). Omit it (standalone server, tests) and the
    ``watchers`` block is empty — never degraded.
    """
    app = FastAPI(
        title="Memstem",
        version=memstem.__version__,
        description="Local HTTP API over Memstem's vault + index.",
    )

    # CORS for renderer-based clients on loopback (browser tabs, future
    # Electron-style extensions). Loopback-only deployment makes this safe;
    # cross-origin restrictions still apply to anything off the loopback.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://127.0.0.1"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    sc = search_config or SearchConfig()
    hc = hygiene_config or HygieneConfig()
    # Build the configured reranker (ADR 0017) once at app construction so
    # every /search call reuses its HTTP client + cache. None when disabled,
    # which Search turns into the NoOp passthrough.
    reranker = build_reranker(
        enabled=sc.reranker.enabled,
        provider=sc.reranker.provider,
        model=sc.reranker.model,
        base_url=sc.reranker.base_url,
        api_key_env=sc.reranker.api_key_env,
    )
    search = Search(vault=vault, index=index, embedder=embedder, reranker=reranker)
    default_rerank_top_n = effective_rerank_top_n(
        sc.rerank_top_n, reranker_enabled=sc.reranker.enabled
    )
    log_client = "http" if hc.query_log_enabled else None

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Health probe: version, embed-queue state, watcher liveness, hygiene.

        ``status`` is computed, not hardcoded: it reports ``"degraded"`` when
        embeddings are failing (``embed_queue.failed > 0``), a watcher thread
        has died, or a state read errors, and ``"ok"`` otherwise. Previously
        this returned a literal ``"ok"`` and an ``embedder`` flag that only
        meant "an embedder was configured at startup" — so the daemon stayed
        green for months while every embed silently failed. ``embed_queue``
        exposes the ``{pending, failed, total}`` the embed worker tracks so
        the failure is visible to monitoring.

        The ``watchers`` block reports each adapter's watchdog observer
        thread (B4): ``true`` healthy, ``false`` dead (problem
        ``watcher_dead:<name>`` — file events are silently dropped; the
        periodic reconcile still catches the records up, on a delay),
        ``null`` not running (startup, or nothing to watch).

        The ``hygiene`` block reports last-run timestamps per stage and any
        stages currently mid-cycle; ``loop_enabled`` reflects the config flag.
        """
        from memstem.hygiene.state import snapshot as hygiene_snapshot

        problems: list[str] = []

        watchers_block: dict[str, bool | None] = {}
        for adapter in adapters or ():
            alive = adapter.watcher_alive()
            watchers_block[adapter.name] = alive
            if alive is False:
                problems.append(f"watcher_dead:{adapter.name}")

        hygiene_block: dict[str, Any] = {"loop_enabled": hc.loop_enabled}
        try:
            hygiene_block.update(hygiene_snapshot(index.db, lock=index.lock))
        except Exception as exc:
            # A state read shouldn't fail, but if it does, don't 500 — report it.
            hygiene_block["error"] = f"{type(exc).__name__}: {exc}"
            problems.append("hygiene_state_unreadable")

        embed_block: dict[str, Any] = {"configured": embedder is not None}
        try:
            stats = index.queue_stats()
            embed_block.update(stats)
            if stats.get("failed", 0) > 0:
                problems.append("embed_failures")
        except Exception as exc:
            embed_block["error"] = f"{type(exc).__name__}: {exc}"
            problems.append("embed_queue_unreadable")

        return {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "version": memstem.__version__,
            "vault": str(vault.root),
            "embedder": embedder is not None,
            "embed_queue": embed_block,
            "watchers": watchers_block,
            "hygiene": hygiene_block,
        }

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": memstem.__version__}

    @app.post("/search", response_model=list[SearchHit])
    async def do_search(req: SearchRequest) -> list[SearchHit]:
        """Hybrid keyword + semantic search across memories and skills."""

        # search.search() makes blocking embedder/reranker HTTP calls (up to
        # the embedder's ~120s timeout) and synchronous SQLite reads. Run it in
        # a worker thread so a slow or hung backend can't freeze the daemon's
        # event loop (ingestion, hygiene, /health all share it). Index.lock
        # keeps the shared connection safe across threads.
        def _run() -> list[SearchHit]:
            results = search.search(
                query=req.query,
                limit=req.limit,
                types=req.types,
                rrf_k=req.rrf_k if req.rrf_k is not None else sc.rrf_k,
                bm25_weight=(req.bm25_weight if req.bm25_weight is not None else sc.bm25_weight),
                vector_weight=(
                    req.vector_weight if req.vector_weight is not None else sc.vector_weight
                ),
                importance_weight=(
                    req.importance_weight
                    if req.importance_weight is not None
                    else sc.importance_weight
                ),
                type_bias=(req.type_bias if req.type_bias is not None else sc.type_bias),
                mmr_lambda=(req.mmr_lambda if req.mmr_lambda is not None else sc.mmr_lambda),
                rerank_top_n=(
                    req.rerank_top_n if req.rerank_top_n is not None else default_rerank_top_n
                ),
                log_client=log_client,
                log_max_rows=hc.query_log_max_rows,
            )
            return [_serialize_result(r) for r in results]

        return await asyncio.to_thread(_run)

    @app.get("/memory/{id_or_path:path}", response_model=MemoryDetail)
    async def get_memory(id_or_path: str) -> MemoryDetail:
        """Retrieve a single memory by vault-relative path or by id."""

        # Synchronous file read + a logged DB write; offload off the event loop
        # like /search so a slow disk can't stall the daemon. log_get holds
        # Index.lock for the shared connection (now running in a worker thread).
        def _run() -> MemoryDetail:
            try:
                memory = vault.read(id_or_path)
                if log_client is not None:
                    log_get(
                        index.db,
                        memory_id=str(memory.id),
                        client=f"{log_client}:get",
                        max_rows=hc.query_log_max_rows,
                        lock=index.lock,
                    )
                return _serialize_memory(memory)
            except MemoryNotFoundError:
                pass
            # Route through `Index.get_path` so the read holds the Index lock —
            # a bare `index.db.execute(...)` here races the embed worker on the
            # shared connection (`InterfaceError: bad parameter or other API
            # misuse`).
            path = index.get_path(id_or_path)
            if path is None:
                raise HTTPException(status_code=404, detail=f"no memory for {id_or_path!r}")
            memory = vault.read(path)
            if log_client is not None:
                log_get(
                    index.db,
                    memory_id=str(memory.id),
                    client=f"{log_client}:get",
                    max_rows=hc.query_log_max_rows,
                    lock=index.lock,
                )
            return _serialize_memory(memory)

        return await asyncio.to_thread(_run)

    return app


async def serve(
    config: HttpServerConfig,
    vault: Vault,
    index: Index,
    embedder: Embedder | None = None,
    *,
    search_config: SearchConfig | None = None,
    hygiene_config: HygieneConfig | None = None,
    adapters: Sequence[Adapter] | None = None,
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

    app = build_app(
        vault,
        index,
        embedder,
        search_config=search_config,
        hygiene_config=hygiene_config,
        adapters=adapters,
    )
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
