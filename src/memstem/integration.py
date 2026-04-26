"""Wire Memstem into Claude Code and OpenClaw client config files.

Four idempotent edits:

1. `register_mcp_server` adds a `mcpServers.<name>` block to a Claude
   Code user config (`~/.claude.json` by default), preserving other
   servers and unrelated keys.
2. `remove_legacy_mcp_server` strips a stale `mcpServers.<name>` entry
   from `~/.claude/settings.json`. Earlier Memstem versions wrote the
   registration there; current Claude Code releases read MCP server
   definitions from `~/.claude.json` instead, so the legacy entry is
   inert and worth cleaning up.
3. `apply_directive` keeps a versioned `<!-- memstem:directive v1 -->`
   block in a CLAUDE.md file, replacing the existing block in place if
   one is present and appending it otherwise.
4. `remove_flipclaw_hook` strips the `claude-code-bridge.py` SessionEnd
   entry from `settings.json` so the legacy capture pipeline stops
   firing once Memstem is live.

Each function writes a `.bak` next to the file before editing, returns
a `Change` describing what happened, and is safe to re-run. Callers can
pass `dry_run=True` to preview the diff without writing.

This module is filesystem-only and has no async or network deps; tests
should drive it with `tmp_path` rather than touching real `~/` files.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DIRECTIVE_BEGIN = "<!-- memstem:directive v1 -->"
DIRECTIVE_END = "<!-- /memstem:directive -->"

DIRECTIVE_BLOCK = f"""{DIRECTIVE_BEGIN}
## Memory access (Memstem)

For retrieval-style queries — "what did we decide about X?", "what's
the plan for Y?", "do we have a skill for Z?" — search Memstem first
via `memstem_search` (MCP) or `memstem search "query"` (CLI). It
indexes every agent's memory + skills + Claude Code sessions with
hybrid keyword + semantic search; a `grep` can't.

For specific known files — `~/<agent>/MEMORY.md`, today's daily log,
a specific SKILL.md you've been told to follow — read directly. That's
faster and Memstem isn't trying to replace direct file access.

Tools: memstem_search, memstem_get, memstem_list_skills,
memstem_get_skill, memstem_upsert.
{DIRECTIVE_END}
"""

DEFAULT_MCP_SERVER_NAME = "memstem"
DEFAULT_MCP_SERVER_ENTRY: dict[str, Any] = {
    "type": "stdio",
    "command": "memstem",
    "args": ["mcp"],
    "env": {},
}

FLIPCLAW_BRIDGE_MARKER = "claude-code-bridge.py"

_DIRECTIVE_PATTERN = re.compile(
    re.escape(DIRECTIVE_BEGIN) + r".*?" + re.escape(DIRECTIVE_END),
    re.DOTALL,
)


@dataclass
class Change:
    """Result of an integration edit."""

    path: Path
    action: str  # "noop" | "created" | "updated"
    message: str = ""
    diff: str = ""
    backup_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.action != "noop"


def _backup(path: Path) -> Path:
    """Copy `path` to `path.bak` (overwriting any existing .bak). Returns the .bak path."""
    bak = (
        path.with_suffix(path.suffix + ".bak")
        if path.suffix
        else path.with_name(path.name + ".bak")
    )
    bak.write_bytes(path.read_bytes())
    return bak


def _diff(label: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (before)",
            tofile=f"{label} (after)",
            n=2,
        )
    )


def register_mcp_server(
    settings_path: Path,
    *,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
    entry: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> Change:
    """Add `mcpServers.<server_name>` to a Claude Code `settings.json`.

    Other servers and unrelated keys are preserved. If the entry already
    matches the desired value, this is a no-op. The file is created if
    missing, with just the `mcpServers` key.
    """
    desired_entry = dict(entry) if entry is not None else dict(DEFAULT_MCP_SERVER_ENTRY)

    if settings_path.exists():
        before = settings_path.read_text(encoding="utf-8")
        if before.strip():
            try:
                data = json.loads(before)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{settings_path} is not valid JSON: {exc}") from exc
        else:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{settings_path} must contain a JSON object at the top level")
    else:
        before = ""
        data = {}

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{settings_path}: 'mcpServers' is present but not an object")

    if servers.get(server_name) == desired_entry:
        return Change(
            path=settings_path,
            action="noop",
            message=f"{server_name} already registered",
        )

    servers[server_name] = desired_entry
    after = json.dumps(data, indent=2) + "\n"

    diff = _diff(str(settings_path), before, after)

    if dry_run:
        return Change(
            path=settings_path,
            action="updated" if before else "created",
            message=f"would register {server_name}",
            diff=diff,
        )

    backup_path: Path | None = None
    if settings_path.exists():
        backup_path = _backup(settings_path)
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings_path.write_text(after, encoding="utf-8")
    return Change(
        path=settings_path,
        action="updated" if before else "created",
        message=f"registered {server_name}",
        diff=diff,
        backup_path=backup_path,
    )


def apply_directive(
    claude_md_path: Path,
    *,
    directive: str = DIRECTIVE_BLOCK,
    dry_run: bool = False,
    create_if_missing: bool = False,
) -> Change:
    """Insert or update the Memstem directive block in a CLAUDE.md file.

    Looks for `<!-- memstem:directive v1 -->...<!-- /memstem:directive -->`.
    Replaces the block in place if present, appends it otherwise. The
    surrounding content is left untouched. If the file doesn't exist the
    call is a no-op unless `create_if_missing=True`.
    """
    block = directive.rstrip() + "\n"

    if not claude_md_path.exists():
        if not create_if_missing:
            return Change(
                path=claude_md_path,
                action="noop",
                message="file does not exist; skipped (use create_if_missing=True to create)",
            )
        before = ""
        after = block
        diff = _diff(str(claude_md_path), before, after)
        if dry_run:
            return Change(
                path=claude_md_path,
                action="created",
                message="would create file with directive block",
                diff=diff,
            )
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        claude_md_path.write_text(after, encoding="utf-8")
        return Change(
            path=claude_md_path,
            action="created",
            message="created with directive block",
            diff=diff,
        )

    before = claude_md_path.read_text(encoding="utf-8")
    match = _DIRECTIVE_PATTERN.search(before)

    if match:
        existing = match.group(0)
        if existing.rstrip() == block.rstrip():
            return Change(
                path=claude_md_path,
                action="noop",
                message="directive block already current",
            )
        after = before[: match.start()] + block.rstrip() + before[match.end() :]
    else:
        # Append, ensuring a blank line between existing content and the block.
        sep = "" if before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
        after = before + sep + block

    diff = _diff(str(claude_md_path), before, after)
    if dry_run:
        return Change(
            path=claude_md_path,
            action="updated",
            message="would update directive block" if match else "would append directive block",
            diff=diff,
        )

    backup_path = _backup(claude_md_path)
    claude_md_path.write_text(after, encoding="utf-8")
    return Change(
        path=claude_md_path,
        action="updated",
        message="updated directive block" if match else "appended directive block",
        diff=diff,
        backup_path=backup_path,
    )


def remove_flipclaw_hook(
    settings_path: Path,
    *,
    marker: str = FLIPCLAW_BRIDGE_MARKER,
    dry_run: bool = False,
) -> Change:
    """Remove FlipClaw's `claude-code-bridge.py` SessionEnd hook from `settings.json`.

    Walks `hooks.SessionEnd[*].hooks[*]` and drops command entries whose
    command contains `marker`. Empty groups and the empty `SessionEnd`
    list are pruned. No-op if no matching entry is found.
    """
    if not settings_path.exists():
        return Change(
            path=settings_path,
            action="noop",
            message="settings.json does not exist",
        )

    before = settings_path.read_text(encoding="utf-8")
    try:
        data = json.loads(before) if before.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{settings_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path} must contain a JSON object at the top level")

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return Change(path=settings_path, action="noop", message="no hooks block")

    session_end = hooks.get("SessionEnd")
    if not isinstance(session_end, list):
        return Change(path=settings_path, action="noop", message="no SessionEnd hooks")

    removed = 0
    new_groups: list[Any] = []
    for group in session_end:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            new_groups.append(group)
            continue
        kept_inner = []
        for h in inner:
            if isinstance(h, dict) and marker in str(h.get("command", "")):
                removed += 1
                continue
            kept_inner.append(h)
        if kept_inner:
            new_group = dict(group)
            new_group["hooks"] = kept_inner
            new_groups.append(new_group)
        # else drop the group entirely (no remaining hooks)

    if removed == 0:
        return Change(
            path=settings_path,
            action="noop",
            message=f"no SessionEnd hook matched marker '{marker}'",
        )

    if new_groups:
        hooks["SessionEnd"] = new_groups
    else:
        hooks.pop("SessionEnd", None)
    if not hooks:
        data.pop("hooks", None)

    after = json.dumps(data, indent=2) + "\n"
    diff = _diff(str(settings_path), before, after)

    if dry_run:
        return Change(
            path=settings_path,
            action="updated",
            message=f"would remove {removed} matching SessionEnd hook(s)",
            diff=diff,
            extra={"removed": removed},
        )

    backup_path = _backup(settings_path)
    settings_path.write_text(after, encoding="utf-8")
    return Change(
        path=settings_path,
        action="updated",
        message=f"removed {removed} matching SessionEnd hook(s)",
        diff=diff,
        backup_path=backup_path,
        extra={"removed": removed},
    )


def remove_legacy_mcp_server(
    legacy_settings_path: Path,
    *,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
    dry_run: bool = False,
) -> Change:
    """Remove a stale `mcpServers.<server_name>` entry from a legacy settings file.

    Earlier Memstem versions wrote the MCP server registration to
    `~/.claude/settings.json`. Current Claude Code releases read MCP
    server definitions from `~/.claude.json` instead, so the legacy
    entry is inert and misleading. This strips it (and removes the
    `mcpServers` key entirely if it becomes empty as a result). No-op
    if the file or entry is absent.
    """
    if not legacy_settings_path.exists():
        return Change(
            path=legacy_settings_path,
            action="noop",
            message="legacy settings file does not exist",
        )

    before = legacy_settings_path.read_text(encoding="utf-8")
    try:
        data = json.loads(before) if before.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{legacy_settings_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{legacy_settings_path} must contain a JSON object at the top level")

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server_name not in servers:
        return Change(
            path=legacy_settings_path,
            action="noop",
            message=f"no legacy '{server_name}' entry to remove",
        )

    del servers[server_name]
    if not servers:
        del data["mcpServers"]

    after = json.dumps(data, indent=2) + "\n"
    diff = _diff(str(legacy_settings_path), before, after)

    if dry_run:
        return Change(
            path=legacy_settings_path,
            action="updated",
            message=f"would remove legacy '{server_name}' entry",
            diff=diff,
        )

    backup_path = _backup(legacy_settings_path)
    legacy_settings_path.write_text(after, encoding="utf-8")
    return Change(
        path=legacy_settings_path,
        action="updated",
        message=f"removed legacy '{server_name}' entry",
        diff=diff,
        backup_path=backup_path,
    )


def claude_md_targets_for_openclaw(workspace_or_file: Path) -> list[Path]:
    """Resolve a `--openclaw` argument to one or more CLAUDE.md paths.

    If the argument is a CLAUDE.md file, return it. If it's a workspace
    directory containing a CLAUDE.md, return that path. Otherwise return
    an empty list (caller decides whether to warn or error).
    """
    p = workspace_or_file.expanduser()
    if p.is_file():
        return [p]
    if p.is_dir():
        candidate = p / "CLAUDE.md"
        if candidate.is_file():
            return [candidate]
    return []


__all__ = [
    "DEFAULT_MCP_SERVER_ENTRY",
    "DEFAULT_MCP_SERVER_NAME",
    "DIRECTIVE_BEGIN",
    "DIRECTIVE_BLOCK",
    "DIRECTIVE_END",
    "FLIPCLAW_BRIDGE_MARKER",
    "Change",
    "apply_directive",
    "claude_md_targets_for_openclaw",
    "register_mcp_server",
    "remove_flipclaw_hook",
    "remove_legacy_mcp_server",
]
