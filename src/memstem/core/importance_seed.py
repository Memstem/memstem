"""Heuristic importance seed at ingest time (ADR 0008 Tier 1, PR-A).

Computes a 0.0-1.0 importance score from cheap signals available at
ingest:

- **Type weight** — skill 0.7 / decision 0.6 / distillation 0.7 /
  reflection 0.7 / memory 0.5 / fact 0.5 / daily 0.4 / project 0.5 /
  person 0.5 / session 0.3.
- **Recency** — linear decay from 1.0 at creation to 0.5 at 90 days,
  constant after.
- **Length penalty** — bodies under 100 chars drop 0.1 (probably not
  useful on their own).

ADR 0008 also specifies an *inbound wikilink density* signal that
requires a vault walk; that signal is deferred to PR-A.1 (or computed
during ``memstem reindex --reseed-importance`` once cheap to do in
batch). The seed is correct without it — wikilinks are an additive
signal that improves discrimination, not a base requirement.

The result is clamped to [0.0, 1.0] and never set below ``MIN_FLOOR``
to keep search-side ranking from collapsing un-loved records below
relevance noise.

This module is pure: no I/O, no SQLite, no LLM. The pipeline calls
:func:`compute_seed` once per record before writing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from memstem.core.frontmatter import MemoryType

# ─── Type weights ─────────────────────────────────────────────────────

# Per ADR 0008 Tier 1. Skills + decisions + distillations + reflections
# are intentional learnings; sessions are conversational and most of
# their content is incidental. Memories and facts sit between.
TYPE_WEIGHTS: Final[dict[MemoryType, float]] = {
    MemoryType.SKILL: 0.7,
    MemoryType.DECISION: 0.6,
    MemoryType.DISTILLATION: 0.7,
    MemoryType.MEMORY: 0.5,
    MemoryType.PROJECT: 0.5,
    MemoryType.PERSON: 0.5,
    MemoryType.DAILY: 0.4,
    MemoryType.SESSION: 0.3,
}
"""Base score per memory type. Records of unknown types fall back to
:data:`DEFAULT_TYPE_WEIGHT` so future enum additions don't crash the
seed."""

DEFAULT_TYPE_WEIGHT: Final[float] = 0.5
"""Neutral type weight for unknown / future enum values."""

# ─── Recency ─────────────────────────────────────────────────────────

RECENCY_FULL_DAYS: Final[float] = 0.0
"""Records younger than this many days score the full recency weight."""

RECENCY_DECAY_DAYS: Final[float] = 90.0
"""Above this age (in days), recency contribution is constant at
:data:`RECENCY_FLOOR`."""

RECENCY_FLOOR: Final[float] = 0.5
"""Minimum recency contribution. Old records still get half-credit so
they can compete on importance even when they're not fresh."""

RECENCY_MAX: Final[float] = 1.0
"""Maximum recency contribution (at age=0)."""

RECENCY_WEIGHT: Final[float] = 0.3
"""How much recency moves the final score relative to type weight.
The seed is::

    seed = type_weight + RECENCY_WEIGHT * (recency - RECENCY_FLOOR)

so a maximally-recent record gets up to ``+RECENCY_WEIGHT * 0.5 = +0.15``
on top of its type weight, while an ancient record is neutral on the
recency axis."""

# ─── Length penalty ─────────────────────────────────────────────────

LENGTH_THRESHOLD_CHARS: Final[int] = 100
"""Bodies shorter than this incur :data:`LENGTH_PENALTY`."""

LENGTH_PENALTY: Final[float] = 0.1
"""Penalty applied to very short bodies — they're rarely useful on
their own."""

# ─── Bounds ──────────────────────────────────────────────────────────

MIN_FLOOR: Final[float] = 0.1
"""Hard floor on the seed. We never want a record at 0 because that
would let the search-side multiplier ``(1 + 0.2 * importance)`` give
it a strictly worse score than an un-scored record at 0.5."""

MAX_CEILING: Final[float] = 1.0
"""Hard ceiling. Pinned records cap at 1.0 already; the seed should
never exceed this on its own."""


def _recency(age_days: float) -> float:
    """Return the recency contribution in [RECENCY_FLOOR, RECENCY_MAX].

    Linear decay from RECENCY_MAX at age=RECENCY_FULL_DAYS to
    RECENCY_FLOOR at age=RECENCY_DECAY_DAYS, then constant.
    """
    if age_days <= RECENCY_FULL_DAYS:
        return RECENCY_MAX
    if age_days >= RECENCY_DECAY_DAYS:
        return RECENCY_FLOOR
    span = RECENCY_DECAY_DAYS - RECENCY_FULL_DAYS
    progress = (age_days - RECENCY_FULL_DAYS) / span
    return RECENCY_MAX - (RECENCY_MAX - RECENCY_FLOOR) * progress


def _type_weight(memory_type: MemoryType | str) -> float:
    """Look up the type weight, falling back to :data:`DEFAULT_TYPE_WEIGHT`.

    Accepts either a :class:`MemoryType` or a raw string (some callers
    have the string before validation completes).
    """
    if isinstance(memory_type, MemoryType):
        return TYPE_WEIGHTS.get(memory_type, DEFAULT_TYPE_WEIGHT)
    try:
        return TYPE_WEIGHTS.get(MemoryType(memory_type), DEFAULT_TYPE_WEIGHT)
    except ValueError:
        return DEFAULT_TYPE_WEIGHT


def compute_seed(
    *,
    memory_type: MemoryType | str,
    body_length: int,
    created: datetime,
    now: datetime | None = None,
) -> float:
    """Compute the heuristic importance seed for a record.

    Inputs are deliberately minimal so the seed is testable in
    isolation — no Memory or Frontmatter object required. The pipeline
    extracts these fields and calls this function before writing the
    record.

    Args:
        memory_type: The record's type (``"memory"``, ``"skill"``, etc.).
        body_length: Number of characters in the body.
        created: When the record was originally created (timezone-aware
            UTC datetime).
        now: Override the "current time" used for recency math. Defaults
            to ``datetime.now(tz=UTC)``. Tests pass a fixed value for
            determinism.

    Returns:
        Float in ``[MIN_FLOOR, MAX_CEILING]``.
    """
    moment = now or datetime.now(tz=UTC)
    age_days = max(0.0, (moment - created).total_seconds() / 86400.0)

    base = _type_weight(memory_type)
    recency_contribution = RECENCY_WEIGHT * (_recency(age_days) - RECENCY_FLOOR)
    length_contribution = -LENGTH_PENALTY if body_length < LENGTH_THRESHOLD_CHARS else 0.0

    score = base + recency_contribution + length_contribution
    return max(MIN_FLOOR, min(MAX_CEILING, score))


__all__ = [
    "DEFAULT_TYPE_WEIGHT",
    "LENGTH_PENALTY",
    "LENGTH_THRESHOLD_CHARS",
    "MAX_CEILING",
    "MIN_FLOOR",
    "RECENCY_DECAY_DAYS",
    "RECENCY_FLOOR",
    "RECENCY_FULL_DAYS",
    "RECENCY_MAX",
    "RECENCY_WEIGHT",
    "TYPE_WEIGHTS",
    "compute_seed",
]
