"""Auto-discover OpenClaw agents and Claude Code targets at install time.

Used by the `memstem init` wizard to populate `_meta/config.yaml` with
sensible defaults: which agent workspaces exist, which have non-empty
memory/skill content, where Claude Code stores sessions, and which
agent-agnostic rules files (HARD-RULES.md) exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from memstem.config import (
    AdaptersConfig,
    ClaudeCodeAdapterConfig,
    OpenClawAdapterConfig,
)

# Top-level files the adapter already handles via dedicated layout fields
# or the agent-agnostic shared_files path. Excluded from extras discovery
# so we don't double-count them.
_EXTRA_FILES_DEDICATED = frozenset(
    {
        "MEMORY.md",  # layout.memory_md
        "CLAUDE.md",  # layout.claude_md
        "HARD-RULES.md",  # adapters.openclaw.shared_files
    }
)

# Filename patterns for files that are *.md but aren't stable system
# references — dated snapshots, one-off incidents, recovery docs. Operators
# can still add them by hand if they want them indexed.
_EXTRA_FILES_NOISY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.*_FULL_\d{4}-\d{2}-\d{2}\.md$"),
    re.compile(r"^.*_REFERENCE_\d{4}-\d{2}-\d{2}\.md$"),
    re.compile(r"^.*-status-report-\d{4}-\d{2}-\d{2}\.md$"),
    re.compile(r"^INCIDENT-\d{4}-\d{2}-\d{2}\.md$"),
    re.compile(r"^RECOVERY-.+\.md$"),
)

# Files larger than this are presumed to be append-only logs (e.g. Ari's
# ``DREAMS.md`` at 286 KB, ``MAINTENANCE.md`` at 35 KB). Indexing them
# churns embeddings on every edit and produces coarse search hits.
# Operators who actually want them indexed can add them manually.
EXTRA_FILES_SIZE_CAP_BYTES = 50_000


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


def discover_workspace_extras(workspace: Path) -> list[str]:
    """Return curated workspace-relative top-level ``.md`` filenames
    suitable for ``OpenClawLayout.extra_files``.

    Walks the immediate workspace directory only; subdirectories
    (``memory/``, ``skills/``, etc.) are handled by their own layout
    fields. Filters out:

    - Files already covered by dedicated layout / shared paths
      (``MEMORY.md``, ``CLAUDE.md``, ``HARD-RULES.md``).
    - Dated snapshots and one-off incident / recovery docs (see
      :data:`_EXTRA_FILES_NOISY_PATTERNS`).
    - Files larger than :data:`EXTRA_FILES_SIZE_CAP_BYTES` (presumed
      append-only logs).

    Returns alphabetically sorted filenames so wizard output is
    deterministic.
    """
    if not workspace.is_dir():
        return []
    extras: list[str] = []
    for entry in sorted(workspace.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        name = entry.name
        if name in _EXTRA_FILES_DEDICATED:
            continue
        if any(p.match(name) for p in _EXTRA_FILES_NOISY_PATTERNS):
            continue
        try:
            if entry.stat().st_size > EXTRA_FILES_SIZE_CAP_BYTES:
                continue
        except OSError:
            continue
        extras.append(name)
    return extras


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
    """Build a conservative default AdaptersConfig.

    Auto-includes Claude Code paths (single user, no multi-agent ambiguity).
    OpenClaw workspaces and shared files are **not** auto-included — on a
    multi-agent host this would silently index every agent on disk. The
    init wizard surfaces discovered candidates and lets the user opt in
    explicitly; non-interactive installs get a Claude-Code-only config
    and can edit the resulting config.yaml to add OpenClaw workspaces.
    """
    claude_root = discover_claude_code_root(home)
    claude_extras = discover_claude_code_extras(home)
    project_roots = [claude_root] if claude_root is not None else []
    return AdaptersConfig(
        openclaw=OpenClawAdapterConfig(
            agent_workspaces=[],
            shared_files=[],
        ),
        claude_code=ClaudeCodeAdapterConfig(
            project_roots=project_roots,
            extra_files=claude_extras,
        ),
    )


__all__ = [
    "EXTRA_FILES_SIZE_CAP_BYTES",
    "OpenClawCandidate",
    "build_default_adapters_config",
    "discover_claude_code_extras",
    "discover_claude_code_root",
    "discover_openclaw_candidates",
    "discover_shared_files",
    "discover_workspace_extras",
]
