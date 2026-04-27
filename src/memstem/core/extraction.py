"""Write-time noise filter for incoming MemoryRecord objects.

ADR 0011 (`docs/decisions/0011-noise-filter-and-fact-extraction.md`)
locks the v0.2 ingest pipeline to filter known-noise patterns at write
time — before they consume index space, embedder cycles, or downstream
cleanup work.

This module is Phase A only: pure regex / heuristic detection. Phase B
(LLM atomic-fact extraction) lands in PR-D. Boot-echo hash matching
is PR-C. ``TAG_TRANSIENT`` TTL writes are PR-B.

Three pattern kinds ship in PR-A, all DROP-only:

- ``heartbeat``   — PM2 monitor output, ``HEARTBEAT_OK`` markers
- ``cron_output`` — cron job runner artifacts, OpenClaw dream markers
- ``tool_dump``   — long uniform JSON / tool-result blocks, no prose

The mem0 audit (mem0ai/mem0#4573) found these three categories alone
account for >70% of captured noise across 32 days of ingestion.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from memstem.adapters.base import MemoryRecord

logger = logging.getLogger(__name__)


class NoiseAction(StrEnum):
    """What the pipeline should do with a record after filtering."""

    KEEP = "keep"
    DROP = "drop"
    TAG_TRANSIENT = "tag_transient"
    """Persist but stamp ``valid_to`` so the record auto-expires."""


@dataclass(frozen=True)
class NoiseDecision:
    """Result of running the noise filter on a record."""

    action: NoiseAction
    kind: str | None = None
    reason: str | None = None
    ttl_days: int | None = None
    """For ``TAG_TRANSIENT`` decisions: how many days from now ``valid_to`` is set."""


# Heartbeat patterns. Two anchors are enough to catch the common cases
# without false-positiving on legitimate prose that mentions the word.
# The first is the literal string PM2-style monitors emit; the second is
# the bracket marker OpenClaw heartbeat scripts use as a line prefix.
_HEARTBEAT_PATTERNS = (
    re.compile(r"^\s*HEARTBEAT_OK\s*$", re.MULTILINE),
    re.compile(r"^\s*\[heartbeat\]", re.IGNORECASE | re.MULTILINE),
)


# Cron output patterns. Both are specific enough that legitimate prose
# discussing cron is unlikely to false-match.
_CRON_PATTERNS = (
    # Matches `__openclaw_dream__`, `__openclaw_test_dream__`, and the real
    # full markers like `__openclaw_memory_core_short_term_promotion_dream__`.
    re.compile(r"__openclaw_[a-z_]*dream__", re.IGNORECASE),
    re.compile(r"^Running cron job:", re.IGNORECASE | re.MULTILINE),
)


# Tool dump heuristic. A "tool dump" is a body where machine-shaped lines
# (JSON braces, tool-use/tool-result markers) outnumber prose by a wide
# margin. Sessions like that are mostly raw tool output and aren't useful
# memories on their own.
_TOOL_DUMP_RATIO = 0.8
_TOOL_DUMP_MIN_LINES = 6
_TOOL_DUMP_MIN_CHARS = 200

# Lines starting with these constructs are "machine-shaped".
_TOOL_LINE_RE = re.compile(
    r"""^\s*(
        [{}\[\]"]                                      # JSON braces, brackets, quotes
        | "(?:type|content|tool[_\s]?(?:use|result|name|input))"  # JSON field names
        | \[tool_(?:use|result)                        # bracketed tool markers
    )""",
    re.VERBOSE,
)


# Transient task state: phrases that strongly indicate ephemeral state
# ("deploy by Friday", "ship by EOD", "merge by tomorrow"). The deliberately
# narrow shape — verb + "by" + day/EOD marker — is conservative to avoid
# false-tagging long-form content that happens to mention temporal words.
# The TTL is 4 weeks (per ADR 0011 Phase A taxonomy), so even a false positive
# only buries the record after a month rather than dropping it outright.
_TRANSIENT_TASK_RE = re.compile(
    r"""\b
        (?:deploy(?:ing)?|ship(?:ping)?|fix(?:ing)?|merge|merging|
           release|releasing|land(?:ing)?|push(?:ing)?)
        \b[^.\n]{0,40}\bby\b\s+
        (?:tomorrow|today|tonight
           |mon(?:day)?|tues?(?:day)?|wed(?:nesday)?|thur?s?(?:day)?
           |fri(?:day)?|sat(?:urday)?|sun(?:day)?
           |eo[dw]
           |end\s+of\s+(?:day|week|sprint)
           |next\s+(?:week|sprint))
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Automation-log refs: source paths that come from monitoring / heartbeat /
# scheduler infrastructure. Path-shape is more reliable than body-shape
# for these because the bodies can look like anything (JSON output,
# free-form notes, etc.) while their location is structurally stable.
_AUTOMATION_PATH_RE = re.compile(
    r"(?:^|[/\\.])(?:heartbeat|monitoring|pm2[/\\]+logs|cron[/\\]+logs|automation)(?:/|\\)",
    re.IGNORECASE,
)


# How long transient records stay searchable before `valid_to` expires them.
# Brad's preference (per the design conversation): "couple of weeks, or four
# weeks at most." Four weeks is the documented default in ADR 0011's table.
_TRANSIENT_TTL_DAYS = 28


# Boot-echo: re-ingestion of system-prompt files. The mem0 audit found this
# was 52.7% of all junk — by far the largest single category. Detection works
# by hashing the first 1024 bytes of every system-prompt file at daemon start
# (build_boot_echo_hashes) and comparing each incoming record's first 1024
# bytes against the set. A match means the record is a boot-file echo and
# should be dropped.
SYSTEM_PROMPT_FILENAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "MEMORY.md",
    "SOUL.md",
    "USER.md",
    "HARD-RULES.md",
)
_BOOT_ECHO_HEAD_BYTES = 1024

# Directories the boot-echo walk skips on sight. None of them contain
# system-prompt files, and several of them (especially Claude Code's
# `projects` dir under `.claude/`) hold tens to hundreds of thousands of
# session JSONLs that would otherwise dominate the walk. Skipping them
# in-place via `os.walk` avoids descending into them at all.
_BOOT_ECHO_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "site-packages",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        # OpenClaw automation / session dirs — bodies live here but never
        # system-prompt files.
        "sessions",
        "heartbeat",
        "monitoring",
        "session-cache",
    }
)

# Cap descent depth from each root. System-prompt files live at or near
# workspace roots — a workspace's CLAUDE.md is at depth 0, an agent's
# memory/MEMORY.md at depth 1. Beyond a few levels we are walking
# user-data trees that won't contain matches.
_BOOT_ECHO_MAX_DEPTH = 4


def is_heartbeat(body: str) -> bool:
    """Return True if ``body`` matches a known heartbeat pattern."""
    if not body or not body.strip():
        return False
    return any(pattern.search(body) for pattern in _HEARTBEAT_PATTERNS)


def is_cron_output(body: str) -> bool:
    """Return True if ``body`` looks like cron job runner output."""
    if not body or not body.strip():
        return False
    return any(pattern.search(body) for pattern in _CRON_PATTERNS)


def is_tool_dump(body: str) -> bool:
    """Return True if ``body`` is mostly machine-shaped lines, not prose.

    Skipped for short bodies (< 200 chars / 6 non-empty lines) — those
    are too small to classify reliably and short bodies aren't a noise
    problem anyway.
    """
    if not body or len(body) < _TOOL_DUMP_MIN_CHARS:
        return False

    lines = [line for line in body.splitlines() if line.strip()]
    if len(lines) < _TOOL_DUMP_MIN_LINES:
        return False

    machine_lines = sum(1 for line in lines if _TOOL_LINE_RE.match(line))
    return machine_lines / len(lines) >= _TOOL_DUMP_RATIO


def is_transient_task(body: str) -> bool:
    """Return True if ``body`` looks like an ephemeral task statement.

    Matches strongly time-bound phrases like "deploy by Friday" or
    "ship by EOD". Conservative on purpose — the TTL is 4 weeks, so a
    false positive only auto-expires the record after a month, but a
    false positive on a long-form *plan* document would still be
    annoying.
    """
    if not body or not body.strip():
        return False
    return _TRANSIENT_TASK_RE.search(body) is not None


def is_automation_log(ref: str) -> bool:
    """Return True if ``ref`` (the source path) comes from automation infrastructure.

    Path-shape is the reliable signal here — heartbeat / monitoring /
    cron / pm2-log directories. Body-shape varies too much to detect
    reliably from content alone.
    """
    if not ref:
        return False
    return _AUTOMATION_PATH_RE.search(ref) is not None


def _head_hash(data: bytes) -> str:
    return hashlib.sha256(data[:_BOOT_ECHO_HEAD_BYTES]).hexdigest()


def build_boot_echo_hashes(paths: list[Path]) -> frozenset[str]:
    """Return SHA-256 hashes of the first 1KB of every system-prompt file under ``paths``.

    Walks each path with two pruning rules to keep startup fast:

    - **Skip dirs.** Names in :data:`_BOOT_ECHO_SKIP_DIRS` are removed
      from the walk in place. The most important is ``projects`` when
      its parent is ``.claude``: that one directory routinely holds
      hundreds of thousands of session JSONLs and was responsible for
      the ~2-minute startup before this fix.
    - **Max depth.** Walk descends at most :data:`_BOOT_ECHO_MAX_DEPTH`
      levels from each root. System-prompt files live near workspace
      roots; deeper paths are user-data trees that don't contain matches.

    Files that don't exist or can't be read are silently skipped — the
    goal is best-effort detection, not a complete inventory. Empty
    heads are also skipped (they'd hash to a constant value that would
    match every empty record).

    The intended caller is the CLI daemon at startup; the returned
    frozenset is passed to :class:`memstem.core.pipeline.Pipeline` and
    used by :func:`noise_filter` to drop re-ingested boot files.
    """
    target = frozenset(SYSTEM_PROMPT_FILENAMES)
    hashes: set[str] = set()
    for root in paths:
        if not root.exists():
            continue
        root_resolved = root.resolve()
        root_depth = len(root_resolved.parts)
        for dirpath_str, dirnames, filenames in os.walk(root_resolved):
            dirpath = Path(dirpath_str)
            depth = len(dirpath.parts) - root_depth

            if depth >= _BOOT_ECHO_MAX_DEPTH:
                # Process files at this level but don't descend further.
                dirnames[:] = []

            # Prune skip-dir names in place so os.walk won't descend.
            # The "projects under .claude" rule is special-cased so
            # generic "projects" dirs elsewhere are still walked.
            pruned: list[str] = []
            for name in dirnames:
                if name in _BOOT_ECHO_SKIP_DIRS:
                    continue
                if name == "projects" and dirpath.name == ".claude":
                    continue
                pruned.append(name)
            dirnames[:] = pruned

            for fname in filenames:
                if fname not in target:
                    continue
                file_path = dirpath / fname
                try:
                    head = file_path.read_bytes()[:_BOOT_ECHO_HEAD_BYTES]
                except OSError as exc:
                    logger.debug("could not read %s for boot-echo hashing: %s", file_path, exc)
                    continue
                if not head:
                    continue
                hashes.add(_head_hash(head))
    return frozenset(hashes)


def is_boot_echo(body: str, hashes: frozenset[str]) -> bool:
    """Return True if the first 1024 bytes of ``body`` hash to a known system-prompt file."""
    if not body or not hashes:
        return False
    head = body.encode("utf-8")
    if not head:
        return False
    return _head_hash(head) in hashes


def noise_filter(
    record: MemoryRecord,
    boot_echo_hashes: frozenset[str] | None = None,
) -> NoiseDecision:
    """Classify ``record`` as KEEP, DROP, or TAG_TRANSIENT.

    DROP patterns (heartbeat / cron_output / tool_dump / boot_echo) drop
    the record without persisting. TAG_TRANSIENT patterns
    (transient_task / automation_log) stamp ``valid_to`` so the record
    auto-expires after the documented TTL.

    ``boot_echo_hashes`` is optional. When omitted, boot-echo detection
    is skipped (this is the case for tests and offline migrations).
    Production daemons build the set at startup via
    :func:`build_boot_echo_hashes` and pass it through.
    """
    body = record.body

    if is_heartbeat(body):
        return NoiseDecision(
            action=NoiseAction.DROP,
            kind="heartbeat",
            reason="body matches a heartbeat marker",
        )

    if is_cron_output(body):
        return NoiseDecision(
            action=NoiseAction.DROP,
            kind="cron_output",
            reason="body matches a cron job runner marker",
        )

    if is_tool_dump(body):
        return NoiseDecision(
            action=NoiseAction.DROP,
            kind="tool_dump",
            reason="body is mostly machine-shaped lines with negligible prose",
        )

    if boot_echo_hashes is not None and is_boot_echo(body, boot_echo_hashes):
        return NoiseDecision(
            action=NoiseAction.DROP,
            kind="boot_echo",
            reason="body's first 1KB hashes to a known system-prompt file",
        )

    if is_automation_log(record.ref):
        return NoiseDecision(
            action=NoiseAction.TAG_TRANSIENT,
            kind="automation_log",
            reason="record originates from an automation/monitoring path",
            ttl_days=_TRANSIENT_TTL_DAYS,
        )

    if is_transient_task(body):
        return NoiseDecision(
            action=NoiseAction.TAG_TRANSIENT,
            kind="transient_task",
            reason="body matches an ephemeral task-state pattern",
            ttl_days=_TRANSIENT_TTL_DAYS,
        )

    return NoiseDecision(action=NoiseAction.KEEP)


__all__ = [
    "SYSTEM_PROMPT_FILENAMES",
    "NoiseAction",
    "NoiseDecision",
    "build_boot_echo_hashes",
    "is_automation_log",
    "is_boot_echo",
    "is_cron_output",
    "is_heartbeat",
    "is_tool_dump",
    "is_transient_task",
    "noise_filter",
]
