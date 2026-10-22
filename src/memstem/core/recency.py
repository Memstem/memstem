"""Query-time recency weighting (ADR 0054).

A record's search score is multiplied by::

    1 + weight * 2 ** (-age_days / half_life_days)

where ``age_days`` is measured *now*, at search time, from the record's
content date. Nothing is written to the vault or the index, so the factor
is always current and switching it off restores the previous ranking
exactly.

- **Content date** is ``provenance.source_updated`` when present (a
  distillation's original session), else ``updated``. A summary written
  today about a June session is as old as the session.
- **Timeless records** (``timeless_types`` — skills, decisions, people by
  default — and anything ``pinned``) score as if updated today, so an old
  decision that is still in force never sinks below fresh chatter.
- **Historical queries** ("back in June", "what did we originally…",
  "2025") switch the factor off for that search: the caller asked about an
  earlier time, so newer records get no edge.

This module is pure: no I/O. :class:`~memstem.core.search.Search` calls
:func:`recency_multiplier` per materialized hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from memstem.core.storage import Memory

DEFAULT_RECENCY_WEIGHT = 0.10
DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0
DEFAULT_TIMELESS_TYPES: tuple[str, ...] = ("skill", "decision", "person")

_MONTHS = (
    "january|february|march|april|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_HISTORICAL_RE = re.compile(
    r"\b(?:"
    r"(?:19|20)\d\d"  # an explicit year
    rf"|(?:{_MONTHS})"  # a month name
    r"|(?:in|since|before|after|during|early|late|mid)[ -]may"  # "may" only as a month
    r"|may \d"
    r"|\w+ (?:days?|weeks?|months?|years?) ago"
    r"|last (?:year|quarter|spring|summer|fall|autumn|winter)"
    r"|originally|initially|at first|used to|back (?:in|when|then)"
    r"|history of|historically|previously|old version|earlier version"
    r"|first time|when did|before we"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RecencyPolicy:
    """The resolved knobs for one search. Built from ``SearchConfig.recency``."""

    weight: float = DEFAULT_RECENCY_WEIGHT
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS
    timeless_types: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_TIMELESS_TYPES)
    )
    skip_historical_queries: bool = True


def policy_from_config(config: Any) -> RecencyPolicy | None:
    """``SearchConfig.recency`` -> policy, or ``None`` when disabled."""
    if config is None or not getattr(config, "enabled", False):
        return None
    return RecencyPolicy(
        weight=config.weight,
        half_life_days=config.half_life_days,
        timeless_types=frozenset(config.timeless_types),
        skip_historical_queries=config.skip_historical_queries,
    )


def is_historical_query(query: str) -> bool:
    """True when the query names or implies an earlier time."""
    return _HISTORICAL_RE.search(query) is not None


def policy_for_query(policy: RecencyPolicy | None, query: str) -> RecencyPolicy | None:
    """Drop the policy for this search when the query is historical."""
    if policy is None or policy.weight == 0.0:
        return None
    if policy.skip_historical_queries and is_historical_query(query):
        return None
    return policy


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def content_date(memory: Memory) -> datetime:
    """When the content itself was last current (see module docstring)."""
    fm = memory.frontmatter
    if fm.provenance is not None:
        source_updated = _as_utc(getattr(fm.provenance, "source_updated", None))
        if source_updated is not None:
            return source_updated
    updated = _as_utc(fm.updated)
    return updated if updated is not None else fm.updated


def is_timeless(memory: Memory, policy: RecencyPolicy) -> bool:
    fm = memory.frontmatter
    return memory.type.value in policy.timeless_types or getattr(fm, "pinned", False) is True


def recency_multiplier(memory: Memory, now: datetime, policy: RecencyPolicy) -> float:
    """``1 + weight * 2 ** (-age / half_life)``; timeless records get the full boost."""
    if is_timeless(memory, policy):
        return 1.0 + policy.weight
    age_days = max(0.0, (now - content_date(memory)).total_seconds() / 86400.0)
    return 1.0 + policy.weight * float(2.0 ** (-age_days / policy.half_life_days))


__all__ = [
    "DEFAULT_RECENCY_HALF_LIFE_DAYS",
    "DEFAULT_RECENCY_WEIGHT",
    "DEFAULT_TIMELESS_TYPES",
    "RecencyPolicy",
    "content_date",
    "is_historical_query",
    "is_timeless",
    "policy_for_query",
    "policy_from_config",
    "recency_multiplier",
]
