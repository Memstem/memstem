"""Command-line interface for Memstem."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

import memstem
from memstem.adapters.base import MemoryRecord
from memstem.adapters.claude_code import ClaudeCodeAdapter
from memstem.adapters.openclaw import OpenClawAdapter
from memstem.client import (
    DaemonClient,
    DaemonError,
    find_daemon,
)
from memstem.client import (
    SearchHit as DaemonSearchHit,
)
from memstem.config import (
    PROVIDER_PROFILES,
    AdaptersConfig,
    ClaudeCodeAdapterConfig,
    Config,
    EmbeddingConfig,
    OpenClawAdapterConfig,
    OpenClawLayout,
    OpenClawWorkspace,
)
from memstem.core.embed_worker import drain_once, run_workers
from memstem.core.embeddings import (
    Embedder,
    EmbeddingError,
    embed_for,
)
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Vault
from memstem.discovery import (
    build_default_adapters_config,
    discover_claude_code_extras,
    discover_claude_code_root,
    discover_openclaw_candidates,
    discover_shared_files,
    discover_workspace_extras,
)
from memstem.integration import (
    Change,
    apply_directive,
    claude_md_targets_for_openclaw,
    mcp_env_from_embedding,
    openclaw_config_for_workspace,
    register_mcp_server,
    register_openclaw_mcp_server,
    remove_flipclaw_hook,
    remove_legacy_mcp_server,
)
from memstem.progress import phase, set_verbose
from memstem.servers.mcp_server import _Resources, build_server
from memstem.star_nudge import maybe_print as _maybe_print_star_nudge

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIRS = ("memories", "skills", "sessions", "daily", "_meta")
DEFAULT_VAULT_PATH = Path.home() / "memstem-vault"
DEFAULT_OPENCLAW_PATHS = (
    Path.home() / "ari" / "memory",
    Path.home() / "ari" / "skills",
)
DEFAULT_CLAUDE_CODE_PATHS = (Path.home() / ".claude" / "projects",)
DEFAULT_CLAUDE_SETTINGS = Path.home() / ".claude.json"
DEFAULT_LEGACY_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_CLAUDE_USER_MD = Path.home() / ".claude" / "CLAUDE.md"


app = typer.Typer(
    name="memstem",
    help="Unified memory and skill infrastructure for AI agents.",
    no_args_is_help=True,
)

auth_app = typer.Typer(
    name="auth",
    help="Manage stored API keys for embedder providers.",
    no_args_is_help=True,
)
app.add_typer(auth_app)

hygiene_app = typer.Typer(
    name="hygiene",
    help="Run vault hygiene tasks (importance bumps, dedup audits, etc.).",
    no_args_is_help=True,
)
app.add_typer(hygiene_app)


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


def _maybe_embedder(config: Config) -> Embedder | None:
    """Build the configured embedder; return None on failure (logged)."""
    try:
        return embed_for(config.embedding)
    except EmbeddingError as exc:
        logger.warning("embedder unavailable: %s", exc)
        return None
    except Exception as exc:  # connection refused, DNS, ...
        logger.warning("embedder unavailable: %s", exc)
        return None


def _embedding_signature(config: Config) -> str:
    """Stable string identifying the embedder configuration.

    Used by the pipeline + worker to detect provider/model switches and
    decide whether existing vectors are still valid. Format is
    ``"<provider>:<model>:<dimensions>"`` — three things that, when
    combined, uniquely determine the vector space we're embedding into.
    """
    e = config.embedding
    return f"{e.provider}:{e.model}:{e.dimensions}"


def _run_init_wizard(home: Path) -> AdaptersConfig:
    """Interactive wizard: ask which OpenClaw agents and Claude Code paths to ingest.

    OpenClaw workspaces and their shared files default to **not** included —
    on a multi-agent host you usually want to scope the vault to one or two
    agents, not every workspace discovery turns up. Claude Code paths default
    to included since they're per-user and unambiguous.
    """
    candidates = discover_openclaw_candidates(home)

    workspaces: list[OpenClawWorkspace] = []
    if candidates:
        typer.echo(f"\nFound {len(candidates)} OpenClaw agent candidate(s):")
        for cand in candidates:
            typer.echo(f"  {cand.tag:<10} — {cand.describe()}")
        typer.echo("")
        for cand in candidates:
            include = typer.confirm(f"Include {cand.tag}?", default=False)
            if not include:
                continue
            extras = discover_workspace_extras(cand.workspace)
            layout = OpenClawLayout()
            if extras:
                listing = ", ".join(extras)
                if typer.confirm(
                    f"  Index {len(extras)} top-level system file(s) ({listing})?",
                    default=True,
                ):
                    layout = OpenClawLayout(extra_files=list(extras))
            workspaces.append(OpenClawWorkspace(path=cand.workspace, tag=cand.tag, layout=layout))
    else:
        typer.echo("\nNo OpenClaw agents found.")

    shared_candidates = discover_shared_files(home)
    chosen_shared: list[Path] = []
    for shared in shared_candidates:
        if typer.confirm(f"Include shared file {shared}?", default=False):
            chosen_shared.append(shared)

    claude_root = discover_claude_code_root(home)
    project_roots: list[Path] = []
    if claude_root is not None and typer.confirm(
        f"Include Claude Code sessions from {claude_root}?", default=True
    ):
        project_roots.append(claude_root)

    extras_found = discover_claude_code_extras(home)
    chosen_extras: list[Path] = []
    for extra in extras_found:
        if typer.confirm(f"Include Claude Code instructions {extra}?", default=True):
            chosen_extras.append(extra)

    return AdaptersConfig(
        openclaw=OpenClawAdapterConfig(
            agent_workspaces=workspaces,
            shared_files=chosen_shared,
        ),
        claude_code=ClaudeCodeAdapterConfig(
            project_roots=project_roots,
            extra_files=chosen_extras,
        ),
    )


@app.command()
def init(
    vault_path: Annotated[str, typer.Argument(help="Path to create the vault at")],
    force: Annotated[bool, typer.Option(help="Overwrite an existing config.yaml")] = False,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            "-y",
            help=(
                "Skip prompts; write a Claude-Code-only config. "
                "OpenClaw workspaces are not auto-included — "
                "edit config.yaml or re-run `memstem init` interactively to add them."
            ),
        ),
    ] = False,
    home: Annotated[
        str | None,
        typer.Option(help="Home directory to scan for agents (default: $HOME)"),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help=(
                "Embedder provider to pre-populate the config with. "
                f"Known: {', '.join(sorted(PROVIDER_PROFILES))}. "
                "Default: ollama (local, no API key). For cloud "
                "providers also run `memstem auth set <provider> <key>` "
                "after init."
            ),
        ),
    ] = None,
) -> None:
    """Initialize a new Memstem vault, with an optional setup wizard."""
    path = Path(vault_path).expanduser().resolve()
    for sub in DEFAULT_VAULT_DIRS:
        (path / sub).mkdir(parents=True, exist_ok=True)
    cfg_path = path / "_meta" / "config.yaml"
    if cfg_path.exists() and not force:
        typer.echo(f"config.yaml exists at {cfg_path}; use --force to overwrite")
        raise typer.Exit(0)

    if provider is not None:
        try:
            embedding_cfg = EmbeddingConfig.for_provider(provider)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
    else:
        embedding_cfg = EmbeddingConfig()

    home_path = Path(home).expanduser() if home else Path.home()
    if non_interactive:
        adapters = build_default_adapters_config(home_path)
        typer.echo(
            f"Auto-selected {len(adapters.openclaw.agent_workspaces)} OpenClaw "
            f"workspace(s), {len(adapters.openclaw.shared_files)} shared file(s), "
            f"{len(adapters.claude_code.project_roots)} Claude Code root(s)."
        )
    else:
        adapters = _run_init_wizard(home_path)

    cfg = Config(vault_path=path, adapters=adapters, embedding=embedding_cfg)
    cfg_path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    typer.echo(f"\ninitialized vault at {path}")
    typer.echo(f"config:  {cfg_path}")
    typer.echo(f"embedder: {embedding_cfg.provider} ({embedding_cfg.model})")
    if embedding_cfg.api_key_env:
        typer.echo(
            f"NOTE: {embedding_cfg.provider} needs an API key. "
            f"Run `memstem auth set {embedding_cfg.provider} <key>` "
            f"or export ${embedding_cfg.api_key_env}."
        )
    typer.echo(f"Run `memstem doctor --vault {path}` to verify.")
    _maybe_print_star_nudge(typer.echo)


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
    no_daemon: Annotated[
        bool,
        typer.Option(
            "--no-daemon",
            help=(
                "Skip the daemon delegation probe and open the SQLite "
                "index directly. Useful for debugging the direct path; "
                "normally the CLI auto-detects a running daemon."
            ),
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v",
            "--verbose",
            help=(
                "Print phase markers (start/done with elapsed seconds) "
                "to stderr. Useful for diagnosing slow searches."
            ),
        ),
    ] = False,
) -> None:
    """One-shot hybrid search of the vault.

    When a local daemon is reachable on loopback and is serving the
    same vault, the CLI delegates to its HTTP `/search` endpoint —
    that hits the daemon's already-open SQLite connection and warm
    embedder, returning in tens of milliseconds regardless of vault
    size. When no daemon is running (or `--no-daemon` is passed), the
    CLI opens the index directly and runs `Search` itself. The two
    paths return identical results; only latency differs.
    """
    set_verbose(verbose)
    with phase("search"):
        cfg = _load_config(_resolve_vault_path(vault))

        if not no_daemon:
            with phase("daemon-probe") as probe_details:
                client = find_daemon(cfg)
                probe_details["found"] = client is not None
            if client is not None:
                try:
                    _search_via_daemon(
                        client,
                        cfg,
                        query=query,
                        limit=limit,
                        types=list(types) if types else None,
                    )
                    return
                except DaemonError as exc:
                    logger.warning("daemon /search failed (%s); falling back to direct DB", exc)
                finally:
                    client.close()

        _search_via_direct_db(
            cfg,
            query=query,
            limit=limit,
            types=list(types) if types else None,
        )


def _search_via_daemon(
    client: DaemonClient,
    cfg: Config,
    *,
    query: str,
    limit: int,
    types: list[str] | None,
) -> None:
    """Render a search result list fetched from the daemon. Kept
    separate from `_search_via_direct_db` so the two transports stay
    symmetrical and easy to reason about — both end in the same
    `_print_search_hits` shape."""
    with phase("daemon-search") as details:
        hits = client.search(
            query,
            limit=limit,
            types=types,
            rrf_k=cfg.search.rrf_k,
            bm25_weight=cfg.search.bm25_weight,
            vector_weight=cfg.search.vector_weight,
            importance_weight=cfg.search.importance_weight,
        )
        details["results"] = len(hits)
    _print_search_hits(hits)


def _search_via_direct_db(
    cfg: Config,
    *,
    query: str,
    limit: int,
    types: list[str] | None,
) -> None:
    """Open the index directly and run `Search`. Used when no daemon
    is reachable, or when the user passes `--no-daemon`."""
    vault_obj = Vault(cfg.vault_path)
    with phase("connect"):
        index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)
    try:
        with phase("direct-search") as details:
            results = Search(vault_obj, index, embedder).search(
                query,
                limit=limit,
                types=types,
                rrf_k=cfg.search.rrf_k,
                bm25_weight=cfg.search.bm25_weight,
                vector_weight=cfg.search.vector_weight,
                importance_weight=cfg.search.importance_weight,
                log_client="cli" if cfg.hygiene.query_log_enabled else None,
                log_max_rows=cfg.hygiene.query_log_max_rows,
            )
            details["results"] = len(results)
    finally:
        index.close()

    if not results:
        typer.echo("(no results)")
        return
    for r in results:
        title = r.memory.frontmatter.title or "(untitled)"
        typer.echo(f"[{r.score:.4f}] {r.memory.type.value:<8} {title}  ({r.memory.path})")


def _print_search_hits(hits: list[DaemonSearchHit]) -> None:
    """Render daemon SearchHit objects in the same shape the direct-DB
    path uses. Pulled out so the two code paths produce identical
    output — anything else is a regression that breaks scripts piping
    the CLI."""
    if not hits:
        typer.echo("(no results)")
        return
    for h in hits:
        title = h.title or "(untitled)"
        typer.echo(f"[{h.score:.4f}] {h.type:<8} {title}  ({h.path})")


@app.command()
def reindex(
    vault: str | None = typer.Option(None, help="Vault path override"),
    embed: bool = typer.Option(
        True, help="Enqueue every record for re-embedding (run `memstem embed` to drain)"
    ),
) -> None:
    """Rebuild the index from the canonical vault.

    Re-walks every markdown file, replaces its memories/tags/links/FTS5
    rows, and (with ``--embed``) enqueues each record for re-embedding.
    The actual embedding happens via the queue worker — run `memstem
    embed` for a one-shot drain or `memstem daemon` to drain
    continuously. Use this after switching embedding providers.
    """
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    try:
        count = 0
        for memory in vault_obj.walk():
            index.upsert(memory)
            if embed:
                index.enqueue_embed(str(memory.id))
            count += 1
        typer.echo(f"reindexed {count} memories")
        if embed:
            stats = index.queue_stats()
            typer.echo(
                f"queue: {stats['pending']} pending — run `memstem embed` to drain "
                f"or `memstem daemon` to drain continuously."
            )
    finally:
        index.close()


@app.command()
def embed(
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="Reset records previously marked failed (max retries exceeded) before draining",
        ),
    ] = False,
    batch_size: Annotated[
        int,
        typer.Option(help="Records pulled per worker iteration"),
    ] = 0,
) -> None:
    """Drain the embedding queue once (then exit).

    Useful after a fresh `memstem migrate` or `memstem reindex`, or to
    backfill embeddings overnight without running the full daemon.
    For continuous draining, use `memstem daemon` instead.
    """
    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)
    if embedder is None:
        typer.echo("no embedder configured (or unavailable). Aborting.")
        index.close()
        raise typer.Exit(1)

    if retry_failed:
        n = index.reset_failed_queue()
        typer.echo(f"reset {n} failed record(s) to pending")

    stats_before = index.queue_stats()
    typer.echo(f"queue: {stats_before['pending']} pending, {stats_before['failed']} failed")
    if stats_before["pending"] == 0:
        typer.echo("nothing to embed.")
        embedder.close()
        index.close()
        return

    bs = batch_size or cfg.embedding.batch_size

    def _progress(n: int) -> None:
        typer.echo(f"  ... {n} records embedded")

    try:
        result = asyncio.run(
            drain_once(
                vault=vault_obj,
                index=index,
                embedder=embedder,
                batch_size=bs,
                on_progress=_progress,
                embedding_signature=_embedding_signature(cfg),
            )
        )
        stats_after = index.queue_stats()
        typer.echo(
            f"\nDone. Processed {result['processed']}; remaining: "
            f"{stats_after['pending']} pending, {stats_after['failed']} failed."
        )
    finally:
        embedder.close()
        index.close()


@app.command()
def migrate(
    apply: Annotated[
        bool, typer.Option("--apply/--dry-run", help="Actually write to the vault")
    ] = False,
    days: Annotated[int, typer.Option(help="Claude Code session lookback window in days")] = 30,
    vault: Annotated[
        str | None,
        typer.Option(help="Vault path override (else MEMSTEM_VAULT or ~/memstem-vault)"),
    ] = None,
    openclaw: Annotated[
        list[str] | None,
        typer.Option(
            help="OpenClaw paths (overrides config; defaults to ~/ari/memory + ~/ari/skills)"
        ),
    ] = None,
    claude_root: Annotated[
        str | None,
        typer.Option(help="Claude Code projects root (defaults to ~/.claude/projects)"),
    ] = None,
    no_embed: Annotated[
        bool,
        typer.Option(
            "--no-embed",
            help=(
                "Skip embedding during migration. Records are still written to the "
                "vault and FTS5-indexed; run `memstem reindex` later to backfill "
                "vectors. Useful for fast bulk imports on CPU-only Ollama."
            ),
        ),
    ] = False,
    progress_every: Annotated[
        int,
        typer.Option(
            help="Print a progress line every N records during --apply (0 = quiet)",
        ),
    ] = 25,
) -> None:
    """One-shot import of FlipClaw / Ari memory into the Memstem vault.

    Default mode is dry-run (counts + sample preview, no writes). Pass
    `--apply` to actually persist. Re-runs are safe — the pipeline
    upserts by `(source, ref)`.
    """
    # Lazy import to break a cli<->migrate cycle: migrate.py reuses cli
    # helpers, so import only when this command actually runs.
    from memstem.migrate import main as _migrate_main

    _migrate_main(
        apply=apply,
        days=days,
        vault=vault,
        openclaw=openclaw,
        claude_root=claude_root,
        no_embed=no_embed,
        progress_every=progress_every,
    )


@app.command()
def mcp(
    vault: str | None = typer.Option(None, help="Vault path override"),
) -> None:
    """Run the Memstem MCP server on stdio.

    Resources (vault, index, embedder) are loaded lazily on the first
    tool call. The MCP handshake (``initialize`` + ``tools/list``)
    completes immediately so clients with short connection timeouts
    (OpenClaw's bundle-mcp defaults to 30s) don't trip while a large
    index is opening.
    """
    cfg = _load_config(_resolve_vault_path(vault))
    resources = _Resources.lazy(
        build_vault=lambda: Vault(cfg.vault_path),
        build_index=lambda: _open_index(cfg),
        build_embedder=lambda: _maybe_embedder(cfg),
    )
    server = build_server(
        resources=resources,
        search_config=cfg.search,
        hygiene_config=cfg.hygiene,
        idle_timeout_seconds=cfg.mcp.idle_timeout_seconds,
    )
    try:
        server.run()
    finally:
        if resources.index_initialized:
            resources.index.close()


def _doctor_check(label: str, ok_status: bool, detail: str = "") -> bool:
    mark = "✓" if ok_status else "✗"
    suffix = f"  ({detail})" if detail else ""
    typer.echo(f"  {mark} {label}{suffix}")
    return ok_status


def _doctor_run(cfg: Config) -> int:
    """Returns the count of failed checks."""
    failures = 0

    py_ok = sys.version_info >= (3, 11)
    if not _doctor_check(
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
        py_ok,
        "" if py_ok else "need 3.11+",
    ):
        failures += 1

    if not _doctor_check(f"memstem {memstem.__version__}", True):
        failures += 1

    vault_ok = cfg.vault_path.is_dir()
    if not _doctor_check(f"Vault: {cfg.vault_path}", vault_ok):
        failures += 1

    cfg_path = cfg.vault_path / "_meta" / "config.yaml"
    if not _doctor_check(f"Config: {cfg_path}", cfg_path.is_file()):
        failures += 1

    if vault_ok:
        try:
            idx = _open_index(cfg)
            try:
                _doctor_check("Index opens cleanly", True)
                stats = idx.queue_stats()
                detail = f"{stats['pending']} pending, {stats['failed']} failed"
                _doctor_check("Embed queue", True, detail)
            finally:
                idx.close()
        except Exception as exc:
            _doctor_check("Index opens cleanly", False, str(exc))
            failures += 1

    provider = cfg.embedding.provider
    label = f"{provider} ({cfg.embedding.model})"
    try:
        embedder = embed_for(cfg.embedding)
        vec = embedder.embed("doctor probe")
        embedder.close()
        _doctor_check(label, True, f"{len(vec)} dims")
    except EmbeddingError as exc:
        _doctor_check(label, False, str(exc))
        failures += 1
    except Exception as exc:
        _doctor_check(label, False, str(exc))
        failures += 1

    oc = cfg.adapters.openclaw
    if oc.agent_workspaces:
        for ws in oc.agent_workspaces:
            ws_path = Path(ws.path).expanduser()
            if not _doctor_check(
                f"OpenClaw workspace: {ws_path} (tag={ws.tag})",
                ws_path.is_dir(),
                "" if ws_path.is_dir() else "directory missing",
            ):
                failures += 1
            for rel in ws.layout.extra_files:
                extra_path = ws_path / rel
                if not _doctor_check(
                    f"OpenClaw extra: {extra_path} (tag={ws.tag})",
                    extra_path.is_file(),
                    "" if extra_path.is_file() else "file missing",
                ):
                    failures += 1
    for shared in oc.shared_files:
        sp = Path(shared).expanduser()
        if not _doctor_check(
            f"OpenClaw shared: {sp}",
            sp.is_file(),
            "" if sp.is_file() else "file missing",
        ):
            failures += 1

    cc = cfg.adapters.claude_code
    for root in cc.project_roots:
        rp = Path(root).expanduser()
        if not _doctor_check(
            f"Claude Code root: {rp}",
            rp.is_dir(),
            "" if rp.is_dir() else "directory missing",
        ):
            failures += 1
    for extra in cc.extra_files:
        ep = Path(extra).expanduser()
        if not _doctor_check(
            f"Claude Code extra: {ep}",
            ep.is_file(),
            "" if ep.is_file() else "file missing",
        ):
            failures += 1

    return failures


@app.command()
def doctor(
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
) -> None:
    """Verify the install: Python, vault, index, Ollama, adapter targets."""
    cfg = _load_config(_resolve_vault_path(vault))
    typer.echo(f"Memstem doctor (vault={cfg.vault_path}):\n")
    failures = _doctor_run(cfg)
    typer.echo()
    if failures > 0:
        typer.echo(f"{failures} issue(s). Run with --vault to point at a different vault.")
        raise typer.Exit(1)
    typer.echo("All checks passed.")
    _maybe_print_star_nudge(typer.echo)


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


def _build_claude_adapter(cfg: Config) -> tuple[ClaudeCodeAdapter, list[Path]]:
    """Build the Claude Code adapter from config.

    Returns `(adapter, project_paths)`. The adapter is constructed with any
    `extra_files` from config; `project_paths` are the JSONL session roots.
    """
    cc = cfg.adapters.claude_code
    paths = (
        [Path(p).expanduser() for p in cc.project_roots]
        if cc.project_roots
        else list(DEFAULT_CLAUDE_CODE_PATHS)
    )
    extras = [Path(p).expanduser() for p in cc.extra_files]
    return ClaudeCodeAdapter(extra_files=extras), paths


async def _run_daemon(
    vault_obj: Vault,
    index: Index,
    embedder: Embedder | None,
    workers: int,
    batch_size: int,
    openclaw_adapter: OpenClawAdapter,
    openclaw_paths: list[Path],
    claude_adapter: ClaudeCodeAdapter,
    claude_paths: list[Path],
    embedding_signature: str = "",
    http_config: Any = None,
    search_config: Any = None,
    hygiene_config: Any = None,
) -> None:
    # Build the boot-echo hash set up front: walk every watched workspace +
    # extra-files location for system-prompt files (CLAUDE.md, MEMORY.md,
    # SOUL.md, USER.md, HARD-RULES.md), hash the first 1KB. Records whose
    # first 1KB hashes to one of these are dropped at ingest as boot echoes
    # (ADR 0011 PR-C — biggest single category in the mem0 audit at 52.7%).
    from memstem.core.extraction import build_boot_echo_hashes

    boot_echo_paths = list({p.expanduser().resolve() for p in (*openclaw_paths, *claude_paths)})
    boot_echo_hashes = build_boot_echo_hashes(boot_echo_paths)
    if boot_echo_hashes:
        logger.info(
            "boot-echo hash table built: %d unique system-prompt heads across %d paths",
            len(boot_echo_hashes),
            len(boot_echo_paths),
        )

    pipeline = Pipeline(
        vault_obj,
        index,
        embedding_signature=embedding_signature,
        boot_echo_hashes=boot_echo_hashes,
    )

    await _reconcile_into_pipeline(
        pipeline,
        openclaw_adapter.reconcile(openclaw_paths),
        label="openclaw",
    )
    await _reconcile_into_pipeline(
        pipeline,
        claude_adapter.reconcile(claude_paths),
        label="claude-code",
    )

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(_drain_into_pipeline(pipeline, openclaw_adapter.watch(openclaw_paths))),
        asyncio.create_task(_drain_into_pipeline(pipeline, claude_adapter.watch(claude_paths))),
    ]
    if embedder is not None:
        tasks.append(
            asyncio.create_task(
                run_workers(
                    workers,
                    vault=vault_obj,
                    index=index,
                    embedder=embedder,
                    batch_size=batch_size,
                    embedding_signature=embedding_signature,
                )
            )
        )
    else:
        logger.warning("no embedder configured — queue will fill but never drain")

    if http_config is not None and getattr(http_config, "enabled", False):
        from memstem.servers.http_server import serve as serve_http

        tasks.append(
            asyncio.create_task(
                serve_http(
                    http_config,
                    vault_obj,
                    index,
                    embedder,
                    search_config=search_config,
                    hygiene_config=hygiene_config,
                )
            )
        )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()


def _print_change(change: Change, dry_run: bool) -> None:
    prefix = "would " if dry_run else ""
    if change.action == "noop":
        typer.echo(f"  · {change.path}: {change.message}")
        return
    verb = {"created": "create", "updated": "update"}.get(change.action, change.action)
    typer.echo(f"  ✓ {change.path}: {prefix}{verb} ({change.message})")
    if dry_run and change.diff:
        for line in change.diff.splitlines():
            typer.echo(f"    {line}")


def _resolve_openclaw_sources(cfg: Config, overrides: list[str] | None) -> list[Path]:
    """The raw `--openclaw` overrides, or every configured workspace path."""
    if overrides:
        return [Path(p).expanduser() for p in overrides]
    return [Path(ws.path).expanduser() for ws in cfg.adapters.openclaw.agent_workspaces]


def _resolve_openclaw_targets(cfg: Config, overrides: list[str] | None) -> list[Path]:
    """Resolve `--openclaw` overrides (or vault config workspaces) to CLAUDE.md paths."""
    sources = _resolve_openclaw_sources(cfg, overrides)
    targets: list[Path] = []
    for src in sources:
        resolved = claude_md_targets_for_openclaw(src)
        if resolved:
            targets.extend(resolved)
        else:
            typer.echo(f"  · {src}: no CLAUDE.md found, skipping")
    return targets


def _resolve_openclaw_configs(cfg: Config, overrides: list[str] | None) -> list[Path]:
    """Resolve `--openclaw` overrides (or vault config workspaces) to openclaw.json paths."""
    sources = _resolve_openclaw_sources(cfg, overrides)
    configs: list[Path] = []
    for src in sources:
        resolved = openclaw_config_for_workspace(src)
        if resolved is not None:
            configs.append(resolved)
        else:
            typer.echo(f"  · {src}: no openclaw.json found, skipping MCP registration")
    return configs


@app.command("connect-clients")
def connect_clients(
    claude_code: Annotated[
        bool,
        typer.Option(
            "--claude-code/--no-claude-code",
            help="Register Memstem in ~/.claude.json and patch ~/.claude/CLAUDE.md",
        ),
    ] = True,
    openclaw: Annotated[
        list[str] | None,
        typer.Option(
            "--openclaw",
            help=(
                "OpenClaw workspace dir or CLAUDE.md path to patch with the "
                "Memstem directive. Repeatable. Defaults to every workspace "
                "in the vault config."
            ),
        ),
    ] = None,
    remove_flipclaw: Annotated[
        bool,
        typer.Option(
            "--remove-flipclaw/--keep-flipclaw",
            help="Strip the FlipClaw claude-code-bridge.py SessionEnd hook from settings.json",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview changes (unified diff) without writing"),
    ] = False,
    settings_path: Annotated[
        str | None,
        typer.Option(
            "--settings",
            help="Override the Claude Code user-config path (default: ~/.claude.json)",
        ),
    ] = None,
    legacy_settings_path: Annotated[
        str | None,
        typer.Option(
            "--legacy-settings",
            help=(
                "Override the legacy Claude Code settings.json path "
                "(default: ~/.claude/settings.json). The legacy file is "
                "scanned for a stale Memstem mcpServers entry and cleaned "
                "up if found."
            ),
        ),
    ] = None,
    claude_md_path: Annotated[
        str | None,
        typer.Option(
            "--claude-md",
            help="Override the Claude Code user CLAUDE.md path (default: ~/.claude/CLAUDE.md)",
        ),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option(help="Vault path override (used to read OpenClaw workspaces)"),
    ] = None,
) -> None:
    """Wire Memstem into Claude Code and OpenClaw client config files.

    Adds the MCP server registration to ~/.claude.json (the location
    current Claude Code releases read for MCP discovery), removes any
    stale entry from the legacy ~/.claude/settings.json, registers
    `mcp.servers.memstem` in each OpenClaw agent's openclaw.json so
    Memstem MCP tools are available to the agent at runtime, ensures
    the Memstem directive block is present in each CLAUDE.md, and
    (with --remove-flipclaw) disables the legacy FlipClaw bridge hook.

    Each edit writes a `.bak` next to the file before changing it.
    Re-running is safe: every step is idempotent.
    """
    cfg = _load_config(_resolve_vault_path(vault))
    settings_target = Path(settings_path).expanduser() if settings_path else DEFAULT_CLAUDE_SETTINGS
    legacy_target = (
        Path(legacy_settings_path).expanduser()
        if legacy_settings_path
        else DEFAULT_LEGACY_CLAUDE_SETTINGS
    )
    user_md = Path(claude_md_path).expanduser() if claude_md_path else DEFAULT_CLAUDE_USER_MD

    typer.echo(f"connect-clients ({'dry-run' if dry_run else 'apply'}):\n")

    # Resolve the embedder's API key once, up front, so we propagate it
    # into every MCP registration the command writes when it is present in
    # the current shell. Memstem MCP can also read keys from
    # ~/.config/memstem/secrets.yaml, so warn only when neither source is set.
    api_key_env_name = cfg.embedding.api_key_env
    mcp_env = mcp_env_from_embedding(api_key_env_name)
    if api_key_env_name and not mcp_env:
        from memstem.auth import get_secret

        if not get_secret(cfg.embedding.provider, api_key_env_name):
            typer.echo(
                f"warning: ${api_key_env_name} is not set in the current shell and "
                f"no stored {cfg.embedding.provider} key was found. Memstem MCP "
                f"entries will be written without an API key — run "
                f"`memstem auth set {cfg.embedding.provider} <key>` or export "
                f"{api_key_env_name} and re-run.\n"
            )

    if claude_code:
        typer.echo(f"Claude Code user config: {settings_target}")
        change = register_mcp_server(settings_target, env=mcp_env, dry_run=dry_run)
        _print_change(change, dry_run)

        typer.echo(f"\nLegacy settings cleanup: {legacy_target}")
        change = remove_legacy_mcp_server(legacy_target, dry_run=dry_run)
        _print_change(change, dry_run)

        typer.echo(f"\nClaude Code instructions: {user_md}")
        # Create the user CLAUDE.md if it doesn't exist — we want every
        # session to see the directive, even on a fresh box.
        change = apply_directive(user_md, dry_run=dry_run, create_if_missing=True)
        _print_change(change, dry_run)
    else:
        typer.echo("Skipping Claude Code (--no-claude-code).")

    openclaw_overrides = list(openclaw) if openclaw else None
    targets = _resolve_openclaw_targets(cfg, openclaw_overrides)
    if targets:
        typer.echo("\nOpenClaw CLAUDE.md targets:")
        for target in targets:
            change = apply_directive(target, dry_run=dry_run)
            _print_change(change, dry_run)

        typer.echo("\nOpenClaw MCP registrations:")
        configs = _resolve_openclaw_configs(cfg, openclaw_overrides)
        for config_path in configs:
            change = register_openclaw_mcp_server(config_path, env=mcp_env, dry_run=dry_run)
            _print_change(change, dry_run)
    elif openclaw:
        typer.echo("\nNo OpenClaw CLAUDE.md targets resolved from --openclaw arguments.")
    elif cfg.adapters.openclaw.agent_workspaces:
        typer.echo("\nNo CLAUDE.md found in any configured OpenClaw workspace.")

    if remove_flipclaw:
        typer.echo(f"\nRemoving FlipClaw SessionEnd hook from {settings_target}:")
        change = remove_flipclaw_hook(settings_target, dry_run=dry_run)
        _print_change(change, dry_run)

    typer.echo("\nDone." if not dry_run else "\nDry run complete; no files written.")


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
    claude_adapter, claude_paths = _build_claude_adapter(cfg)

    typer.echo(f"daemon: vault={cfg.vault_path}")
    if openclaw_adapter.workspaces:
        for ws in openclaw_adapter.workspaces:
            typer.echo(f"  openclaw workspace: {ws.path}  (tag={ws.tag})")
            if ws.layout.extra_files:
                typer.echo(f"    extras: {', '.join(ws.layout.extra_files)}")
    elif openclaw_paths:
        typer.echo(f"  openclaw legacy paths: {', '.join(str(p) for p in openclaw_paths)}")
    if openclaw_adapter.shared_files:
        typer.echo(
            f"  openclaw shared files: {', '.join(str(p) for p in openclaw_adapter.shared_files)}"
        )
    typer.echo(f"  claude-code roots: {', '.join(str(p) for p in claude_paths)}")
    if claude_adapter.extra_files:
        typer.echo(f"  claude-code extras: {', '.join(str(p) for p in claude_adapter.extra_files)}")
    if embedder is not None:
        typer.echo(
            f"  embedder: {cfg.embedding.provider} / {cfg.embedding.model} "
            f"({cfg.embedding.workers} worker(s), batch={cfg.embedding.batch_size})"
        )
    else:
        typer.echo("  embedder: (none — queue will fill but not drain)")

    if cfg.http.enabled:
        typer.echo(f"  http server: http://{cfg.http.host}:{cfg.http.port}")

    try:
        asyncio.run(
            _run_daemon(
                vault_obj=vault_obj,
                index=index,
                embedder=embedder,
                workers=cfg.embedding.workers,
                batch_size=cfg.embedding.batch_size,
                openclaw_adapter=openclaw_adapter,
                openclaw_paths=openclaw_paths,
                claude_adapter=claude_adapter,
                claude_paths=claude_paths,
                embedding_signature=_embedding_signature(cfg),
                http_config=cfg.http,
                search_config=cfg.search,
                hygiene_config=cfg.hygiene,
            )
        )
    except KeyboardInterrupt:
        typer.echo("daemon: stopped")
    finally:
        index.close()


@auth_app.command("set")
def auth_set(
    provider: Annotated[
        str,
        typer.Argument(help="Provider name: openai, gemini, voyage."),
    ],
    key: Annotated[
        str | None,
        typer.Argument(help="API key. Omit to read from stdin (or be prompted)."),
    ] = None,
) -> None:
    """Store an API key in ``~/.config/memstem/secrets.yaml`` (mode 0600).

    Used as a fallback when the corresponding env var (e.g. ``OPENAI_API_KEY``)
    is not set in the shell — so cron, PM2, and headless servers don't need
    their own export.
    """
    from memstem.auth import PROVIDERS, mask, set_secret

    provider_lc = provider.lower()
    if provider_lc not in PROVIDERS:
        typer.echo(
            f"unknown provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}",
            err=True,
        )
        raise typer.Exit(2)

    if key is None:
        if sys.stdin.isatty():
            key = typer.prompt("API key", hide_input=True)
        else:
            key = sys.stdin.read().strip()

    if not key or not key.strip():
        typer.echo("error: empty key", err=True)
        raise typer.Exit(2)

    set_secret(provider_lc, key)
    typer.echo(f"stored {provider_lc}: {mask(key)}")


@auth_app.command("show")
def auth_show(
    provider: Annotated[
        str | None,
        typer.Argument(help="Show one provider, or all known providers if omitted."),
    ] = None,
) -> None:
    """Show stored secrets (masked) and where they came from (env vs file)."""
    from memstem.auth import PROVIDERS, list_secrets, mask

    if provider:
        targets = [provider.lower()]
        if targets[0] not in PROVIDERS:
            typer.echo(
                f"unknown provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}",
                err=True,
            )
            raise typer.Exit(2)
    else:
        targets = sorted(PROVIDERS)

    file_secrets = list_secrets()
    found = False
    for p in targets:
        env_name = PROVIDERS[p]
        env_val = os.environ.get(env_name, "").strip()
        file_val = file_secrets.get(p, "")
        if env_val:
            typer.echo(f"{p}: {mask(env_val)}  (env: {env_name})")
            found = True
        elif file_val:
            typer.echo(f"{p}: {file_val}  (file)")
            found = True
        elif provider:
            typer.echo(f"{p}: not set", err=True)

    if not found and not provider:
        typer.echo(
            "no secrets stored. Try: memstem auth set <provider> <key>",
            err=True,
        )


@auth_app.command("remove")
def auth_remove(
    provider: Annotated[
        str,
        typer.Argument(help="Provider name to remove."),
    ],
) -> None:
    """Remove a stored secret from ``~/.config/memstem/secrets.yaml``."""
    from memstem.auth import PROVIDERS, remove_secret

    provider_lc = provider.lower()
    if provider_lc not in PROVIDERS:
        typer.echo(
            f"unknown provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}",
            err=True,
        )
        raise typer.Exit(2)

    if remove_secret(provider_lc):
        typer.echo(f"removed {provider_lc}")
    else:
        typer.echo(f"{provider_lc} was not stored", err=True)
        raise typer.Exit(1)


@hygiene_app.command("importance")
def hygiene_importance(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "Apply the importance bumps. Default is --dry-run, which "
                "prints the proposed changes without mutating the vault "
                "or advancing the hygiene cursor."
            ),
        ),
    ] = False,
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
) -> None:
    """Bump importance on memories that recently appeared in retrievals.

    Reads the bounded ``query_log`` written by search/get and proposes
    conservative bumps weighted by rank, recency, and exposure kind.
    Defaults to ``--dry-run`` so the first run is always reviewable;
    pass ``--apply`` to persist.

    Skip rules: records whose ``valid_to`` is in the past or whose
    ``deprecated_by`` is set are not bumped. The cursor in
    ``hygiene_state`` advances only on apply, so re-running is
    idempotent.
    """
    from memstem.hygiene.importance import (
        apply_importance_updates,
        compute_importance_updates,
    )

    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    try:
        plan = compute_importance_updates(vault_obj, index)
        if not plan.updates:
            typer.echo("hygiene importance: no bumps proposed.")
            if apply:
                # Still advance the cursor so the next run starts from a
                # fresh window — otherwise empty sweeps re-scan stale
                # rows forever.
                apply_importance_updates(vault_obj, index, plan)
            return
        mode = "applying" if apply else "dry-run"
        typer.echo(f"hygiene importance ({mode}): {len(plan.updates)} bump(s) proposed")
        for update in plan.updates:
            delta = update.proposed - update.current
            reasons = "; ".join(update.reasons) if update.reasons else "—"
            typer.echo(
                f"  {update.memory_id}  "
                f"{update.current:.3f} → {update.proposed:.3f}  (+{delta:.3f})  "
                f"[{reasons}]"
            )
        if apply:
            n = apply_importance_updates(vault_obj, index, plan)
            typer.echo(f"\nhygiene importance: applied {n} bump(s).")
        else:
            typer.echo("\nhygiene importance: dry-run; re-run with --apply to persist these bumps.")
    finally:
        index.close()


@hygiene_app.command("distill")
def hygiene_distill(
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
    min_cluster_size: Annotated[
        int,
        typer.Option(
            help=(
                "Minimum cluster size to qualify as a candidate. "
                "ADR 0008 Tier 2 sets this at 5 by default."
            ),
        ),
    ] = 5,
) -> None:
    """List distillation candidates: clusters of memories that could be summarized.

    First slice (ADR 0008 PR-D): purely a candidate report — walks
    the vault, groups memories by shared topic tags or by ``type=daily``
    + agent + ISO week, and prints any cluster of at least
    ``--min-cluster-size`` records. **Does not call any LLM**, **does
    not mutate the vault**, **does not create distillation records.**

    The LLM-driven distiller that consumes this report and writes
    actual ``type=distillation`` memories ships in a later PR behind
    an explicit config flag.

    Re-running is cheap: candidates whose every member is already
    linked from an existing distillation memory are filtered out.
    """
    from memstem.hygiene.distillation import find_distillation_candidates

    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    candidates = find_distillation_candidates(
        vault_obj,
        min_cluster_size=min_cluster_size,
    )
    if not candidates:
        typer.echo(
            f"hygiene distill: no distillation candidates (min cluster size = {min_cluster_size})."
        )
        return
    typer.echo(
        f"hygiene distill: {len(candidates)} candidate(s) "
        f"(min cluster size = {min_cluster_size}):\n"
    )
    for candidate in candidates:
        typer.echo(
            f"  [{candidate.kind}] {candidate.cluster_id}  "
            f"({candidate.size} members) — {candidate.rationale}"
        )
        for title, path in zip(candidate.member_titles, candidate.member_paths, strict=True):
            typer.echo(f"    · {title}  ({path})")
        typer.echo("")


@hygiene_app.command("dedup-candidates")
def hygiene_dedup_candidates(
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
    min_cosine: Annotated[
        float,
        typer.Option(
            help=(
                "Cosine similarity threshold for candidate pairs. "
                "ADR 0012 Layer 2 default is 0.85 — deliberately "
                "permissive so the LLM judge sees plausible candidates."
            ),
        ),
    ] = 0.85,
    neighbors: Annotated[
        int,
        typer.Option(
            help="How many vec-nearest-neighbors to consider per memory.",
        ),
    ] = 5,
    limit: Annotated[
        int | None,
        typer.Option(help="Cap the report to the top-N strongest pairs."),
    ] = None,
    max_memories: Annotated[
        int | None,
        typer.Option(
            "--max-memories",
            help=(
                "Bounded preview: only consider the first N indexed "
                "memories (sorted by id) as the outer loop. A full scan "
                "issues one vec query per memory — quadratic in vault "
                "size, several tens of seconds on a ~1k-memory vault — "
                "so this flag is the right knob for a smoke test or a "
                "quick spot check. Default is unbounded (full scan)."
            ),
        ),
    ] = None,
) -> None:
    """List near-duplicate candidate pairs by vec similarity.

    First slice of ADR 0012 Layer 2: walks the index's stored chunk
    embeddings, finds memory pairs whose first chunks are within a
    cosine threshold, and reports them. **Read-only.** Does not
    delete, merge, mark, or write anything; the operator reviews the
    report and decides manually until Layer 3 (the LLM judge) lands.

    Pairs where either side is a skill are flagged with `[skill]` so
    the operator can be extra-careful — ADR 0012 routes skill
    candidates through a human review queue rather than auto-merging.

    Note on cost: a full scan is roughly O(N²) in indexed memories,
    not bounded by `--limit` (which only caps the *report*). For a
    bounded preview, pass `--max-memories N`.
    """
    from memstem.hygiene.dedup_candidates import find_dedup_candidate_pairs

    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    try:
        pairs = find_dedup_candidate_pairs(
            vault_obj,
            index,
            min_cosine=min_cosine,
            neighbors_per_memory=neighbors,
            limit=limit,
            max_memories=max_memories,
        )
    finally:
        index.close()

    if not pairs:
        typer.echo(f"hygiene dedup-candidates: no pairs above cosine {min_cosine}.")
        return

    typer.echo(
        f"hygiene dedup-candidates: {len(pairs)} candidate pair(s) (min cosine = {min_cosine}):\n"
    )
    for pair in pairs:
        skill_marker = " [skill]" if pair.involves_skill else ""
        typer.echo(
            f"  cos={pair.cosine:.3f}{skill_marker}  "
            f"{pair.a_title or pair.a_id}  ↔  {pair.b_title or pair.b_id}"
        )
        typer.echo(f"    a: {pair.a_path}  ({pair.a_id})")
        typer.echo(f"    b: {pair.b_path}  ({pair.b_id})")


@hygiene_app.command("dedup-judge")
def hygiene_dedup_judge(
    vault: Annotated[str | None, typer.Option(help="Vault path override")] = None,
    min_cosine: Annotated[
        float,
        typer.Option(
            help=(
                "Cosine threshold for candidate pair generation (passed "
                "through to dedup-candidates). Default 0.85."
            ),
        ),
    ] = 0.85,
    neighbors: Annotated[
        int,
        typer.Option(help="Vec-nearest-neighbors per memory."),
    ] = 5,
    limit: Annotated[
        int | None,
        typer.Option(help="Cap pairs evaluated by the judge."),
    ] = None,
    max_memories: Annotated[
        int | None,
        typer.Option(
            "--max-memories",
            help=(
                "Bounded preview: only consider the first N indexed "
                "memories as the outer loop for candidate generation. "
                "Same semantics as `dedup-candidates --max-memories`."
            ),
        ),
    ] = None,
    enable_llm: Annotated[
        bool,
        typer.Option(
            "--enable-llm/--no-llm",
            help=(
                "Use the configured Ollama model as the judge. Default "
                "off — the audit log is populated with NoOpJudge "
                "(verdict=UNRELATED for every pair) so the operator "
                "can review what would be evaluated before paying "
                "LLM cost."
            ),
        ),
    ] = False,
    ollama_url: Annotated[
        str,
        typer.Option(help="Ollama base URL (used only with --enable-llm)."),
    ] = "http://localhost:11434",
    ollama_model: Annotated[
        str,
        typer.Option(help="Ollama model id (used only with --enable-llm)."),
    ] = "qwen2.5:7b",
) -> None:
    """Judge near-duplicate candidate pairs and append to the audit log.

    First slice of ADR 0012 Layer 3: pulls candidates from the same
    pipeline as `memstem hygiene dedup-candidates`, runs each through
    a judge (NoOpJudge by default; OllamaDedupJudge with `--enable-llm`),
    and writes one row per pair to the `dedup_audit` table with
    `applied = 0`.

    **No vault mutations.** Verdicts sit in the audit table for the
    operator to review. The future resolution PR will flip
    `applied = 1` on rows whose verdict is safe to apply (writing
    `deprecated_by`, `valid_to`, `supersedes`, or `links` to vault
    frontmatter as appropriate). Until then, this is purely an
    inventory + opinion step.
    """
    from memstem.hygiene.dedup_candidates import find_dedup_candidate_pairs
    from memstem.hygiene.dedup_judge import (
        DedupJudge,
        NoOpJudge,
        OllamaDedupJudge,
        judge_pairs,
        write_audit_rows,
    )

    cfg = _load_config(_resolve_vault_path(vault))
    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    try:
        pairs = find_dedup_candidate_pairs(
            vault_obj,
            index,
            min_cosine=min_cosine,
            neighbors_per_memory=neighbors,
            limit=limit,
            max_memories=max_memories,
        )
        if not pairs:
            typer.echo("hygiene dedup-judge: no candidate pairs to judge.")
            return

        judge: DedupJudge
        if enable_llm:
            typer.echo(
                f"hygiene dedup-judge: judging {len(pairs)} pair(s) via "
                f"Ollama ({ollama_url}, model={ollama_model})"
            )
            judge = OllamaDedupJudge(base_url=ollama_url, model=ollama_model)
        else:
            typer.echo(
                f"hygiene dedup-judge: writing {len(pairs)} NoOp audit row(s) "
                "(use --enable-llm to invoke the configured Ollama judge)"
            )
            judge = NoOpJudge()

        results = judge_pairs(pairs, judge=judge)
        n_written = write_audit_rows(index.db, results)
        typer.echo(f"\nhygiene dedup-judge: wrote {n_written} audit row(s).")
        for result in results:
            typer.echo(
                f"  {result.verdict.value:<22}  "
                f"{result.new_id} <-> {result.existing_id}  "
                f"({result.judge}: {result.rationale})"
            )
    finally:
        index.close()


if __name__ == "__main__":
    app()
