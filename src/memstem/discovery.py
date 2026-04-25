"""Auto-discover OpenClaw agents and Claude Code targets at install time.

Used by the `memstem init` wizard to populate `_meta/config.yaml` with
sensible defaults: which agent workspaces exist, which have non-empty
memory/skill content, where Claude Code stores sessions, and which
agent-agnostic rules files (HARD-RULES.md) exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memstem.config import (
    AdaptersConfig,
    ClaudeCodeAdapterConfig,
    OpenClawAdapterConfig,
    OpenClawWorkspace,
)


@dataclass(frozen=True)
class OpenClawCandidate:
    """One agent workspace discovered on disk."""

    workspace: Path
    tag: str
    has_memory_md: bool
    has_claude_md: bool
    memory_files: int
    skill_files: int

    @property
    def has_content(self) -> bool:
        return (
            self.has_memory_md
            or self.has_claude_md
            or self.memory_files > 0
            or self.skill_files > 0
        )

    def describe(self) -> str:
        parts = []
        if self.has_memory_md:
            parts.append("MEMORY.md ✓")
        if self.has_claude_md:
            parts.append("CLAUDE.md ✓")
        parts.append(f"{self.memory_files} memory")
        parts.append(f"{self.skill_files} skills")
        return ", ".join(parts)


def _inspect_workspace(workspace: Path) -> OpenClawCandidate:
    memory_dir = workspace / "memory"
    skills_dir = workspace / "skills"
    memory_files = 0
    skill_files = 0
    if memory_dir.is_dir():
        memory_files = sum(1 for f in memory_dir.rglob("*.md") if f.is_file())
    if skills_dir.is_dir():
        skill_files = sum(1 for f in skills_dir.rglob("SKILL.md") if f.is_file())
    return OpenClawCandidate(
        workspace=workspace,
        tag=workspace.name,
        has_memory_md=(workspace / "MEMORY.md").is_file(),
        has_claude_md=(workspace / "CLAUDE.md").is_file(),
        memory_files=memory_files,
        skill_files=skill_files,
    )


def discover_openclaw_candidates(home: Path | None = None) -> list[OpenClawCandidate]:
    """Find agent workspaces under `home` (default: current user's home).

    A candidate is any direct child of `home` that contains an
    `openclaw.json` file. Sorted alphabetically by tag.
    """
    root = home or Path.home()
    if not root.is_dir():
        return []
    candidates: list[OpenClawCandidate] = []
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        if not (item / "openclaw.json").is_file():
            continue
        candidates.append(_inspect_workspace(item))
    return candidates


def discover_shared_files(home: Path | None = None) -> list[Path]:
    """Find agent-agnostic rules files (e.g. HARD-RULES.md) across workspaces."""
    root = home or Path.home()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        if not (item / "openclaw.json").is_file():
            continue
        rules = item / "HARD-RULES.md"
        if rules.is_file():
            found.append(rules)
    return found


def discover_claude_code_root(home: Path | None = None) -> Path | None:
    """Return `~/.claude/projects` if it exists, else None."""
    root = home or Path.home()
    candidate = root / ".claude" / "projects"
    return candidate if candidate.is_dir() else None


def discover_claude_code_extras(home: Path | None = None) -> list[Path]:
    """Return per-user Claude Code instructions files that exist."""
    root = home or Path.home()
    extras: list[Path] = []
    user_md = root / ".claude" / "CLAUDE.md"
    if user_md.is_file():
        extras.append(user_md)
    return extras


def build_default_adapters_config(home: Path | None = None) -> AdaptersConfig:
    """Build an AdaptersConfig that includes every candidate with non-empty content."""
    workspaces = [
        OpenClawWorkspace(path=c.workspace, tag=c.tag)
        for c in discover_openclaw_candidates(home)
        if c.has_content
    ]
    shared = discover_shared_files(home)
    claude_root = discover_claude_code_root(home)
    claude_extras = discover_claude_code_extras(home)
    project_roots = [claude_root] if claude_root is not None else []
    return AdaptersConfig(
        openclaw=OpenClawAdapterConfig(
            agent_workspaces=workspaces,
            shared_files=shared,
        ),
        claude_code=ClaudeCodeAdapterConfig(
            project_roots=project_roots,
            extra_files=claude_extras,
        ),
    )


__all__ = [
    "OpenClawCandidate",
    "build_default_adapters_config",
    "discover_claude_code_extras",
    "discover_claude_code_root",
    "discover_openclaw_candidates",
    "discover_shared_files",
]
