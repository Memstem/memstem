"""Command-line interface for Memstem."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated

import typer
import yaml

import memstem
from memstem.adapters.base import MemoryRecord
from memstem.adapters.claude_code import ClaudeCodeAdapter
from memstem.adapters.openclaw import OpenClawAdapter
from memstem.config import (
    AdaptersConfig,
    ClaudeCodeAdapterConfig,
    Config,
    OpenClawAdapterConfig,
    OpenClawWorkspace,
)
from memstem.core.embeddings import OllamaEmbedder, chunk_text
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
)
from memstem.integration import (
    Change,
    apply_directive,
    claude_md_targets_for_openclaw,
    register_mcp_server,
    remove_flipclaw_hook,
)
from memstem.servers.mcp_server import build_server

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIRS = ("memories", "skills", "sessions", "daily", "_meta")
DEFAULT_VAULT_PATH = Path.home() / "memstem-vault"
DEFAULT_OPENCLAW_PATHS = (
    Path.home() / "ari" / "memory",
    Path.home() / "ari" / "skills",
)
DEFAULT_CLAUDE_CODE_PATHS = (Path.home() / ".claude" / "projects",)
DEFAULT_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_CLAUDE_USER_MD = Path.home() / ".claude" / "CLAUDE.md"


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


def _run_init_wizard(home: Path) -> AdaptersConfig:
    """Interactive wizard: ask which OpenClaw agents and Claude Code paths to ingest."""
    candidates = discover_openclaw_candidates(home)

    workspaces: list[OpenClawWorkspace] = []
    if candidates:
        typer.echo(f"\nFound {len(candidates)} OpenClaw agent candidates:")
        for cand in candidates:
            typer.echo(f"  {cand.tag:<10} — {cand.describe()}")
        typer.echo("")
        for cand in candidates:
            include = typer.confirm(f"Include {cand.tag}?", default=cand.has_content)
            if include:
                workspaces.append(OpenClawWorkspace(path=cand.workspace, tag=cand.tag))
    else:
        typer.echo("\nNo OpenClaw agents found.")

    shared_candidates = discover_shared_files(home)
    chosen_shared: list[Path] = []
    for shared in shared_candidates:
        if typer.confirm(f"Include shared file {shared}?", default=True):
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
            help="Skip prompts; auto-include every discovered agent with content",
        ),
    ] = False,
    home: Annotated[
        str | None,
        typer.Option(help="Home directory to scan for agents (default: $HOME)"),
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

    cfg = Config(vault_path=path, adapters=adapters)
    cfg_path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    typer.echo(f"\ninitialized vault at {path}")
    typer.echo(f"config:  {cfg_path}")
    typer.echo(f"Run `memstem doctor --vault {path}` to verify.")


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
            idx.close()
            _doctor_check("Index opens cleanly", True)
        except Exception as exc:
            _doctor_check("Index opens cleanly", False, str(exc))
            failures += 1

    if cfg.embedding.provider == "ollama":
        try:
            embedder = OllamaEmbedder(
                model=cfg.embedding.model,
                base_url=cfg.embedding.base_url,
                dimensions=cfg.embedding.dimensions,
            )
            vec = embedder.embed("doctor probe")
            embedder.close()
            _doctor_check(
                f"Ollama at {cfg.embedding.base_url} ({cfg.embedding.model})",
                True,
                f"{len(vec)} dims",
            )
        except Exception as exc:
            _doctor_check(
                f"Ollama at {cfg.embedding.base_url} ({cfg.embedding.model})",
                False,
                str(exc),
            )
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
    embedder: OllamaEmbedder | None,
    openclaw_adapter: OpenClawAdapter,
    openclaw_paths: list[Path],
    claude_adapter: ClaudeCodeAdapter,
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
        claude_adapter.reconcile(claude_paths),
        label="claude-code",
    )

    tasks = [
        asyncio.create_task(_drain_into_pipeline(pipeline, openclaw_adapter.watch(openclaw_paths))),
        asyncio.create_task(_drain_into_pipeline(pipeline, claude_adapter.watch(claude_paths))),
    ]
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


def _resolve_openclaw_targets(cfg: Config, overrides: list[str] | None) -> list[Path]:
    """Resolve `--openclaw` overrides (or vault config workspaces) to CLAUDE.md paths."""
    if overrides:
        sources = [Path(p).expanduser() for p in overrides]
    else:
        sources = [Path(ws.path).expanduser() for ws in cfg.adapters.openclaw.agent_workspaces]
    targets: list[Path] = []
    for src in sources:
        resolved = claude_md_targets_for_openclaw(src)
        if resolved:
            targets.extend(resolved)
        else:
            typer.echo(f"  · {src}: no CLAUDE.md found, skipping")
    return targets


@app.command("connect-clients")
def connect_clients(
    claude_code: Annotated[
        bool,
        typer.Option(
            "--claude-code/--no-claude-code",
            help="Register Memstem in ~/.claude/settings.json and patch ~/.claude/CLAUDE.md",
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
            help="Override the Claude Code settings.json path (default: ~/.claude/settings.json)",
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

    Adds the MCP server registration to settings.json, ensures the
    Memstem directive block is present in each CLAUDE.md, and (with
    --remove-flipclaw) disables the legacy FlipClaw bridge hook.

    Each edit writes a `.bak` next to the file before changing it.
    Re-running is safe: every step is idempotent.
    """
    cfg = _load_config(_resolve_vault_path(vault))
    settings_target = Path(settings_path).expanduser() if settings_path else DEFAULT_CLAUDE_SETTINGS
    user_md = Path(claude_md_path).expanduser() if claude_md_path else DEFAULT_CLAUDE_USER_MD

    typer.echo(f"connect-clients ({'dry-run' if dry_run else 'apply'}):\n")

    if claude_code:
        typer.echo(f"Claude Code settings: {settings_target}")
        change = register_mcp_server(settings_target, dry_run=dry_run)
        _print_change(change, dry_run)

        typer.echo(f"\nClaude Code instructions: {user_md}")
        # Create the user CLAUDE.md if it doesn't exist — we want every
        # session to see the directive, even on a fresh box.
        change = apply_directive(user_md, dry_run=dry_run, create_if_missing=True)
        _print_change(change, dry_run)
    else:
        typer.echo("Skipping Claude Code (--no-claude-code).")

    targets = _resolve_openclaw_targets(cfg, list(openclaw) if openclaw else None)
    if targets:
        typer.echo("\nOpenClaw CLAUDE.md targets:")
        for target in targets:
            change = apply_directive(target, dry_run=dry_run)
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
    elif openclaw_paths:
        typer.echo(f"  openclaw legacy paths: {', '.join(str(p) for p in openclaw_paths)}")
    if openclaw_adapter.shared_files:
        typer.echo(
            f"  openclaw shared files: {', '.join(str(p) for p in openclaw_adapter.shared_files)}"
        )
    typer.echo(f"  claude-code roots: {', '.join(str(p) for p in claude_paths)}")
    if claude_adapter.extra_files:
        typer.echo(f"  claude-code extras: {', '.join(str(p) for p in claude_adapter.extra_files)}")

    try:
        asyncio.run(
            _run_daemon(
                vault_obj=vault_obj,
                index=index,
                embedder=embedder,
                openclaw_adapter=openclaw_adapter,
                openclaw_paths=openclaw_paths,
                claude_adapter=claude_adapter,
                claude_paths=claude_paths,
            )
        )
    except KeyboardInterrupt:
        typer.echo("daemon: stopped")
    finally:
        index.close()


if __name__ == "__main__":
    app()
