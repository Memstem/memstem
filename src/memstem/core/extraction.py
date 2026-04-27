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


@dataclass(frozen=True)
class NoiseDecision:
    """Result of running the noise filter on a record."""

    action: NoiseAction
    kind: str | None = None
    reason: str | None = None


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


def noise_filter(record: MemoryRecord) -> NoiseDecision:
    """Classify ``record`` as KEEP or DROP based on noise patterns.

    PR-A scope: DROP-only patterns. Boot-echo hash matching (PR-C) and
    ``TAG_TRANSIENT`` TTL tagging (PR-B) extend this function in later PRs.
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

    return NoiseDecision(action=NoiseAction.KEEP)


__all__ = [
    "NoiseAction",
    "NoiseDecision",
    "is_cron_output",
    "is_heartbeat",
    "is_tool_dump",
    "noise_filter",
]
