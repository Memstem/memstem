"""Strip vector chunks from records already hidden by an elapsed ``valid_to``.

Expiry (ADR 0011 / ADR 0049) hides a record from default search, but search
filters ``valid_to`` *after* the vec0 KNN scan, so an expired record still
costs every query its chunks. This pass removes those chunks while keeping the
canonical markdown, the ``memories``/FTS rows and ``embed_state`` — the record
stays readable and recoverable, and nothing re-embeds it (the pipeline skips
expired records and ``needs_reembed`` still sees the stored body hash).

Recovery for one record: clear ``valid_to`` in its frontmatter and delete its
``embed_state`` row (or re-save it with changed content); the embed worker then
rebuilds its vectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from memstem.core.index import Index


@dataclass(frozen=True)
class ExpiredVectorHit:
    id: str
    type: str
    title: str | None
    chunks: int


@dataclass(frozen=True)
class StripPlan:
    hits: tuple[ExpiredVectorHit, ...]

    @property
    def chunks(self) -> int:
        return sum(h.chunks for h in self.hits)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def find_expired_with_vectors(
    index: Index,
    *,
    types: frozenset[str] | None = None,
    now: datetime | None = None,
) -> StripPlan:
    """Records whose ``valid_to`` has elapsed and that still hold vector chunks."""
    now = now or datetime.now(tz=UTC)
    with index._lock:
        rows = index.db.execute(
            "SELECT id, type, title, valid_to FROM memories WHERE valid_to IS NOT NULL"
        ).fetchall()
    hits: list[ExpiredVectorHit] = []
    for row in rows:
        if types is not None and row["type"] not in types:
            continue
        valid_to = _parse(row["valid_to"])
        if valid_to is None or valid_to > now:
            continue
        with index._lock:
            n = len(index._vec_chunk_ids(row["id"]))
        if n:
            hits.append(ExpiredVectorHit(row["id"], row["type"], row["title"], n))
    return StripPlan(hits=tuple(hits))


def apply_strip(index: Index, plan: StripPlan) -> int:
    """Strip every planned record's vectors; returns chunks removed."""
    return sum(index.strip_vectors(h.id) for h in plan.hits)


__all__ = ["ExpiredVectorHit", "StripPlan", "apply_strip", "find_expired_with_vectors"]
