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

import re
from dataclasses import dataclass
from enum import StrEnum

from memstem.adapters.base import MemoryRecord


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


def noise_filter(record: MemoryRecord) -> NoiseDecision:
    """Classify ``record`` as KEEP, DROP, or TAG_TRANSIENT.

    DROP patterns (heartbeat / cron_output / tool_dump) shipped in PR-A.
    TAG_TRANSIENT patterns (transient_task / automation_log) ship in
    PR-B and stamp ``valid_to`` so the record auto-expires after the
    documented TTL. Boot-echo hash matching arrives in PR-C.
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
    "NoiseAction",
    "NoiseDecision",
    "is_automation_log",
    "is_cron_output",
    "is_heartbeat",
    "is_tool_dump",
    "is_transient_task",
    "noise_filter",
]
