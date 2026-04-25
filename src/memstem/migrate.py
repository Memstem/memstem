"""One-shot migration from FlipClaw / Ari into a Memstem vault.

Walks the existing on-disk memory under `~/ari/memory/` and
`~/ari/skills/`, plus recent Claude Code sessions in
`~/.claude/projects/`, runs each through the standard pipeline, and
tags every imported record with `flipclaw-migration` for traceability.

Default mode is dry-run (counts + sample preview, no writes). Pass
`--apply` to actually persist the migration.

Usage from the script wrapper:
    python scripts/migrate-from-flipclaw.py
    python scripts/migrate-from-flipclaw.py --apply
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from memstem.adapters.base import MemoryRecord
from memstem.adapters.claude_code import ClaudeCodeAdapter
from memstem.adapters.openclaw import OpenClawAdapter
from memstem.cli import _load_config, _maybe_embedder, _open_index, _resolve_vault_path
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault

logger = logging.getLogger(__name__)

ARI_MEMORY_PATHS = (
    Path.home() / "ari" / "memory",
    Path.home() / "ari" / "skills",
)
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_DAYS = 30
MIGRATION_TAG = "flipclaw-migration"

app = typer.Typer(help="Migrate FlipClaw / Ari memory into a Memstem vault.")


def _is_recent(path: Path, cutoff: datetime) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    return mtime >= cutoff


def tag_for_migration(record: MemoryRecord) -> MemoryRecord:
    """Return a copy of `record` with `flipclaw-migration` added (idempotent)."""
    if MIGRATION_TAG in record.tags:
        return record
    new_tags = [*record.tags, MIGRATION_TAG]
    return record.model_copy(update={"tags": new_tags})


async def collect_openclaw(paths: list[Path]) -> list[MemoryRecord]:
    """Read all OpenClaw markdown files under `paths`, tag, and return."""
    return [tag_for_migration(r) async for r in OpenClawAdapter().reconcile(paths)]


async def _claude_records_in_window(
    root: Path, cutoff: datetime
) -> AsyncGenerator[MemoryRecord, None]:
    async for record in ClaudeCodeAdapter().reconcile([root]):
        if _is_recent(Path(record.ref), cutoff):
            yield record


async def collect_claude(days: int, root: Path) -> list[MemoryRecord]:
    """Read Claude Code sessions whose mtime is within `days` of now."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    return [tag_for_migration(r) async for r in _claude_records_in_window(root, cutoff)]


async def collect_all(
    days: int,
    openclaw_paths: list[Path],
    claude_root: Path,
) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
    """Collect both OpenClaw and Claude Code records into separate lists."""
    return (
        await collect_openclaw(openclaw_paths),
        await collect_claude(days, claude_root),
    )


def _print_summary(name: str, records: list[MemoryRecord], sample: int = 3) -> None:
    typer.echo(f"\n{name}: {len(records)} record(s)")
    if not records:
        return
    typer.echo("  sample:")
    for r in records[:sample]:
        type_ = r.metadata.get("type", "?")
        title = r.title or "(no title)"
        typer.echo(f"    [{type_}] {title}  ({r.ref})")


@app.command()
def main(
    apply: Annotated[bool, typer.Option(help="Actually write to the vault")] = False,
    days: Annotated[
        int,
        typer.Option(help="Claude Code session lookback window in days"),
    ] = DEFAULT_DAYS,
    vault: Annotated[
        str | None,
        typer.Option(help="Vault path override (else MEMSTEM_VAULT or ~/memstem-vault)"),
    ] = None,
    openclaw: Annotated[
        list[str] | None,
        typer.Option(help="OpenClaw paths (defaults to ~/ari/memory and ~/ari/skills)"),
    ] = None,
    claude_root: Annotated[
        str | None,
        typer.Option(help="Claude Code projects root (defaults to ~/.claude/projects)"),
    ] = None,
) -> None:
    """Migrate FlipClaw memory into the Memstem vault."""
    cfg = _load_config(_resolve_vault_path(vault))
    typer.echo(f"vault:  {cfg.vault_path}")
    typer.echo(f"mode:   {'APPLY' if apply else 'DRY-RUN'}")
    typer.echo(f"window: last {days} days for Claude Code sessions")

    openclaw_paths = (
        [Path(p).expanduser() for p in openclaw] if openclaw else list(ARI_MEMORY_PATHS)
    )
    claude_path = Path(claude_root).expanduser() if claude_root else CLAUDE_PROJECTS

    openclaw_records, claude_records = asyncio.run(collect_all(days, openclaw_paths, claude_path))

    _print_summary("openclaw memory + skills", openclaw_records)
    _print_summary("claude-code sessions", claude_records)

    if not apply:
        typer.echo("\nDry-run complete. Re-run with --apply to write.")
        return

    vault_obj = Vault(cfg.vault_path)
    index = _open_index(cfg)
    embedder = _maybe_embedder(cfg)
    try:
        pipeline = Pipeline(vault_obj, index, embedder)
        applied = 0
        for r in openclaw_records + claude_records:
            try:
                pipeline.process(r)
                applied += 1
            except Exception as exc:
                logger.warning("failed %s/%s: %s", r.source, r.ref, exc)
        typer.echo(f"\nApplied: {applied}/{len(openclaw_records) + len(claude_records)} records")
    finally:
        index.close()


__all__ = [
    "ARI_MEMORY_PATHS",
    "CLAUDE_PROJECTS",
    "DEFAULT_DAYS",
    "MIGRATION_TAG",
    "app",
    "collect_all",
    "collect_claude",
    "collect_openclaw",
    "tag_for_migration",
]
