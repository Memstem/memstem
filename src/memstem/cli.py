"""Command-line interface for Memstem."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated

import typer
import yaml

from memstem.adapters.base import MemoryRecord
from memstem.adapters.claude_code import ClaudeCodeAdapter
from memstem.adapters.openclaw import OpenClawAdapter
from memstem.config import Config
from memstem.core.embeddings import OllamaEmbedder, chunk_text
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Vault
from memstem.servers.mcp_server import build_server

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIRS = ("memories", "skills", "sessions", "daily", "_meta")
DEFAULT_VAULT_PATH = Path.home() / "memstem-vault"
DEFAULT_OPENCLAW_PATHS = (
    Path.home() / "ari" / "memory",
    Path.home() / "ari" / "skills",
)
DEFAULT_CLAUDE_CODE_PATHS = (Path.home() / ".claude" / "projects",)


app = typer.Typer(
    name="memstem",
    help="Unified memory and skill infrastructure for AI agents.",
    no_args_is_help=True,
)


def _resolve_vault_path(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("MEMSTEM_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_VAULT_PATH


def _load_config(vault_path: Path) -> Config:
    cfg_path = vault_path / "_meta" / "config.yaml"
    if not cfg_path.is_file():
        return Config(vault_path=vault_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return Config(vault_path=vault_path)
    raw.setdefault("vault_path", str(vault_path))
    return Config.model_validate(raw)


def _open_index(config: Config) -> Index:
    db_path = config.index_path or config.vault_path / "_meta" / "index.db"
    idx = Index(db_path, dimensions=config.embedding.dimensions)
    idx.connect()
    return idx


def _maybe_embedder(config: Config) -> OllamaEmbedder | None:
    if config.embedding.provider != "ollama":
        return None
    try:
        return OllamaEmbedder(
            model=config.embedding.model,
            base_url=config.embedding.base_url,
            dimensions=config.embedding.dimensions,
        )
    except Exception as exc:
        logger.warning("embedder unavailable: %s", exc)
        return None


@app.command()
def init(
    vault_path: str = typer.Argument(..., help="Path to create the vault at"),
    force: bool = typer.Option(False, help="Overwrite an existing config.yaml"),
) -> None:
    """Initialize a new Memstem vault."""
    path = Path(vault_path).expanduser().resolve()
    for sub in DEFAULT_VAULT_DIRS:
        (path / sub).mkdir(parents=True, exist_ok=True)
    cfg_path = path / "_meta" / "config.yaml"
    if cfg_path.exists() and not force:
        typer.echo(f"config.yaml exists at {cfg_path}; use --force to overwrite")
        raise typer.Exit(0)
    cfg = Config(vault_path=path)
    cfg_path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    typer.echo(f"initialized vault at {path}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option(help="Maximum number of results")] = 10,
    types: Annotated[
        list[str] | None,
        typer.Option(help="Filter by memory type(s)"),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option(help="Vault path override"),
    ] = None,
) -> None:
    """One-shot hybrid search of the vault."""
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)
    try:
        results = Search(vault_obj, index, embedder).search(
            query,
            limit=limit,
            types=list(types) if types else None,
        )
    finally:
        index.close()

    if not results:
        typer.echo("(no results)")
        return
    for r in results:
        title = r.memory.frontmatter.title or "(untitled)"
        typer.echo(f"[{r.score:.4f}] {r.memory.type.value:<8} {title}  ({r.memory.path})")


@app.command()
def reindex(
    vault: str | None = typer.Option(None, help="Vault path override"),
    embed: bool = typer.Option(True, help="Re-embed chunks via the configured embedder"),
) -> None:
    """Rebuild the index from the canonical vault."""
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg) if embed else None
    try:
        count = 0
        for memory in vault_obj.walk():
            index.upsert(memory)
            if embedder is not None:
                chunks = chunk_text(memory.body)
                if chunks:
                    try:
                        vecs = embedder.embed_batch(chunks)
                        index.upsert_vectors(str(memory.id), chunks, vecs)
                    except Exception as exc:
                        logger.warning("embed failed for %s: %s", memory.id, exc)
            count += 1
        typer.echo(f"reindexed {count} memories")
    finally:
        index.close()


@app.command()
def mcp(
    vault: str | None = typer.Option(None, help="Vault path override"),
) -> None:
    """Run the Memstem MCP server on stdio."""
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)
    server = build_server(vault_obj, index, embedder)
    try:
        server.run()
    finally:
        index.close()


async def _drain_into_pipeline(
    pipeline: Pipeline,
    stream: AsyncGenerator[MemoryRecord, None],
) -> None:
    async for record in stream:
        try:
            pipeline.process(record)
        except Exception as exc:
            logger.warning("pipeline failed for %s/%s: %s", record.source, record.ref, exc)


async def _reconcile_into_pipeline(
    pipeline: Pipeline,
    stream: AsyncGenerator[MemoryRecord, None],
    label: str,
) -> int:
    count = 0
    async for record in stream:
        try:
            pipeline.process(record)
            count += 1
        except Exception as exc:
            logger.warning("reconcile failed for %s/%s: %s", record.source, record.ref, exc)
    logger.info("reconcile complete (%s): %d records", label, count)
    return count


def _build_openclaw_adapter(cfg: Config) -> tuple[OpenClawAdapter, list[Path]]:
    """Build the OpenClaw adapter from config; fall back to legacy paths.

    Returns `(adapter, fallback_paths)`. `fallback_paths` is empty when
    workspaces are configured (the adapter walks them via its constructor)
    and only used in legacy mode.
    """
    oc = cfg.adapters.openclaw
    if oc.agent_workspaces or oc.shared_files:
        return (
            OpenClawAdapter(
                workspaces=list(oc.agent_workspaces),
                shared_files=[Path(p).expanduser() for p in oc.shared_files],
            ),
            [],
        )
    return OpenClawAdapter(), list(DEFAULT_OPENCLAW_PATHS)


def _build_claude_paths(cfg: Config) -> list[Path]:
    cc = cfg.adapters.claude_code
    if cc.project_roots:
        return [Path(p).expanduser() for p in cc.project_roots]
    return list(DEFAULT_CLAUDE_CODE_PATHS)


async def _run_daemon(
    vault_obj: Vault,
    index: Index,
    embedder: OllamaEmbedder | None,
    openclaw_adapter: OpenClawAdapter,
    openclaw_paths: list[Path],
    claude_paths: list[Path],
) -> None:
    pipeline = Pipeline(vault_obj, index, embedder)

    await _reconcile_into_pipeline(
        pipeline,
        openclaw_adapter.reconcile(openclaw_paths),
        label="openclaw",
    )
    await _reconcile_into_pipeline(
        pipeline,
        ClaudeCodeAdapter().reconcile(claude_paths),
        label="claude-code",
    )

    tasks = [
        asyncio.create_task(_drain_into_pipeline(pipeline, openclaw_adapter.watch(openclaw_paths))),
        asyncio.create_task(
            _drain_into_pipeline(pipeline, ClaudeCodeAdapter().watch(claude_paths))
        ),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()


@app.command()
def daemon(
    vault: str | None = typer.Option(None, help="Vault path override"),
) -> None:
    """Run adapter reconcile + watch loop, ingesting into the vault and index."""
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)

    openclaw_adapter, openclaw_paths = _build_openclaw_adapter(cfg)
    claude_paths = _build_claude_paths(cfg)

    typer.echo(f"daemon: vault={cfg.vault_path}")
    if openclaw_adapter.workspaces:
        for ws in openclaw_adapter.workspaces:
            typer.echo(f"  openclaw workspace: {ws.path}  (tag={ws.tag})")
    elif openclaw_paths:
        typer.echo(f"  openclaw legacy paths: {', '.join(str(p) for p in openclaw_paths)}")
    if openclaw_adapter.shared_files:
        typer.echo(
            f"  openclaw shared files: {', '.join(str(p) for p in openclaw_adapter.shared_files)}"
        )
    typer.echo(f"  claude-code roots: {', '.join(str(p) for p in claude_paths)}")

    try:
        asyncio.run(
            _run_daemon(
                vault_obj=vault_obj,
                index=index,
                embedder=embedder,
                openclaw_adapter=openclaw_adapter,
                openclaw_paths=openclaw_paths,
                claude_paths=claude_paths,
            )
        )
    except KeyboardInterrupt:
        typer.echo("daemon: stopped")
    finally:
        index.close()


if __name__ == "__main__":
    app()
