"""Lift expiries that retired noise-filter rules set before ADR 0058.

ADR 0058 changed the write-time noise filter so it no longer hides
substantive records: the ``transient_task`` body rule ("deploy by Friday")
is retired, and the heartbeat / cron / tool-dump rules keep records that
carry real prose. New filter expiries are stamped ``provenance.noise_expiry``
and the pipeline lifts them by itself once the filter stops tagging the
record. Expiries written before ADR 0058 carry no stamp, so the pipeline
keeps them (it cannot tell them from an expiry set by hand); this pass finds
and lifts them.

A record qualifies when it has a ``valid_to``, is not deprecated or deleted,
carries no ``noise_expiry`` stamp, its body matches one of the old rules
(transient task, heartbeat, cron, tool dump), and the current filter keeps it.
An expiry on a record whose body matches none of those rules was not set by
the filter and is never touched. Lifting clears ``valid_to`` in the markdown
and index and re-queues the record for embedding (its vectors may have been
stripped while it was expired).
"""

from __future__ import annotations

from dataclasses import dataclass

from memstem.adapters.base import MemoryRecord
from memstem.core.extraction import (
    NoiseAction,
    is_cron_output,
    is_heartbeat,
    is_tool_dump,
    is_transient_task,
    noise_filter,
)
from memstem.core.index import Index
from memstem.core.storage import Memory, MemoryNotFoundError, Vault


@dataclass(frozen=True)
class LiftHit:
    id: str
    path: str
    type: str
    title: str | None
    valid_to: str
    old_rule: str


@dataclass(frozen=True)
class LiftPlan:
    hits: tuple[LiftHit, ...]
    examined: int


def _old_rule(body: str) -> str | None:
    if is_transient_task(body):
        return "transient_task"
    if is_heartbeat(body):
        return "heartbeat"
    if is_cron_output(body):
        return "cron_output"
    if is_tool_dump(body):
        return "tool_dump"
    return None


def find_liftable(
    vault: Vault,
    index: Index,
    *,
    openclaw_scheduled_session_ttl_days: int | None = None,
) -> LiftPlan:
    """Plan which unstamped expiries came from a rule the filter no longer applies."""
    with index._lock:
        rows = index.db.execute(
            "SELECT id, path, type, title, valid_to FROM memories "
            "WHERE valid_to IS NOT NULL AND deprecated_by IS NULL AND deleted_at IS NULL"
        ).fetchall()
    hits: list[LiftHit] = []
    for row in rows:
        try:
            memory = vault.read(row["path"])
        except MemoryNotFoundError:
            continue
        fm = memory.frontmatter
        prov = fm.provenance.model_dump() if fm.provenance is not None else {}
        if prov.get("noise_expiry"):
            continue  # stamped: the pipeline lifts it on the next ingest
        rule = _old_rule(memory.body)
        if rule is None:
            continue  # not a filter expiry (set by hand or by another tool)
        record = MemoryRecord(
            source=fm.source,
            ref=str(prov.get("ref") or row["path"]),
            title=fm.title or "",
            body=memory.body,
            tags=list(fm.tags),
            metadata={"type": fm.type.value, "created": fm.created.isoformat()},
        )
        decision = noise_filter(
            record, openclaw_scheduled_session_ttl_days=openclaw_scheduled_session_ttl_days
        )
        if decision.action is not NoiseAction.KEEP:
            continue
        hits.append(
            LiftHit(
                id=str(fm.id),
                path=str(row["path"]),
                type=fm.type.value,
                title=fm.title,
                valid_to=str(row["valid_to"]),
                old_rule=rule,
            )
        )
    return LiftPlan(hits=tuple(hits), examined=len(rows))


def apply_lift(vault: Vault, index: Index, plan: LiftPlan) -> int:
    """Clear ``valid_to`` on every planned record and re-queue it for embedding."""
    lifted = 0
    for hit in plan.hits:
        memory = vault.read(hit.path)
        fm = memory.frontmatter.model_copy(update={"valid_to": None})
        updated = Memory(frontmatter=fm, body=memory.body, path=memory.path)
        vault.write(updated)
        index.upsert(updated)
        index.enqueue_embed(hit.id)
        lifted += 1
    return lifted


__all__ = ["LiftHit", "LiftPlan", "apply_lift", "find_liftable"]
