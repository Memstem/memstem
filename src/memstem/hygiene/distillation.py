"""Distillation candidate generator (ADR 0008 Tier 2, PR-D first slice).

This is the deterministic, **non-destructive** first slice of the
distillation pipeline. It walks the canonical vault, finds clusters of
related memories that *could* be distilled into a single rollup
record, and returns a report. It does **not** call any LLM, does
**not** mutate the vault, and does **not** create distillation
records — those land in a later PR behind an explicit config flag.

Two clustering strategies ship in this slice:

1. **Tag clusters.** Memories sharing a tag of the form ``topic:*``
   are grouped together. Topic tags are the canonical "group these"
   marker the codebase already uses (e.g., ``topic:cloudflare``,
   ``topic:auth``). ``agent:*`` tags are explicitly ignored as
   cluster keys — every record in an agent's workspace shares them,
   so they'd produce one giant cluster per agent.

2. **Daily-log clusters.** ``type=daily`` records from the same
   ``agent:<x>`` tagged workspace within the same ISO calendar week
   are grouped. This catches the common case "summarize my Ari week
   of 2026-W17."

A cluster qualifies as a candidate if it has at least
``min_cluster_size`` members (default 5, per ADR 0008's "min 5
members"). Clusters whose members are already linked from an existing
``type=distillation`` memory are filtered out so re-running the report
doesn't keep re-proposing the same already-handled clusters.

The output (a list of :class:`DistillationCandidate`) is purely
informational. The CLI prints it; future PRs will feed it to the LLM
distiller.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from memstem.core.frontmatter import MemoryType
from memstem.core.storage import Memory, Vault

logger = logging.getLogger(__name__)


DEFAULT_MIN_CLUSTER_SIZE = 5
"""ADR 0008 Tier 2 threshold: clusters smaller than this are too
short for a useful rollup — readers would just read the originals."""


TOPIC_TAG_PREFIX = "topic:"


@dataclass(frozen=True)
class DistillationCandidate:
    """One cluster proposed for distillation.

    ``kind`` identifies the clustering strategy that produced this
    candidate (``"topic"`` for shared topic tags, ``"daily-week"``
    for agent + ISO-week grouped daily logs). The CLI uses it to
    group output by strategy.
    """

    cluster_id: str
    """Stable identifier for this cluster, e.g., ``topic:cloudflare``
    or ``daily:ari/2026-W17``. Stable across runs so a future
    distillation memory can declare ``provenance.ref = cluster_id``."""

    kind: str
    rationale: str
    member_ids: list[str] = field(default_factory=list)
    member_paths: list[str] = field(default_factory=list)
    member_titles: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.member_ids)


def _topic_tags(memory: Memory) -> list[str]:
    """Return the ``topic:*`` tags on this memory (if any)."""
    return [t for t in memory.frontmatter.tags if t.startswith(TOPIC_TAG_PREFIX)]


def _agent_tag(memory: Memory) -> str | None:
    """Return the agent tag (without the ``agent:`` prefix), or ``None``."""
    for t in memory.frontmatter.tags:
        if t.startswith("agent:"):
            return t[len("agent:") :]
    return None


def _iso_week_id(d: date) -> str:
    """ISO calendar week as ``YYYY-Www`` (zero-padded)."""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _already_distilled_member_ids(vault: Vault) -> set[str]:
    """Collect ids that appear in existing distillation memories' ``links``.

    Distillation memories declare their sources via the ``links`` field
    (per ADR 0008's distillation shape). A cluster whose every member
    is already covered is skipped to keep the candidate list fresh.
    """
    covered: set[str] = set()
    for memory in vault.walk():
        if memory.type is not MemoryType.DISTILLATION:
            continue
        for link in memory.frontmatter.links:
            # Links look like ``memory://memories/.../<id>.md`` or just
            # the id. Be permissive: take the last path component
            # without an extension.
            stripped = link.rsplit("/", 1)[-1]
            if stripped.endswith(".md"):
                stripped = stripped[: -len(".md")]
            if stripped:
                covered.add(stripped)
    return covered


def _build_candidate(
    cluster_id: str,
    kind: str,
    rationale: str,
    members: list[Memory],
) -> DistillationCandidate:
    member_ids = [str(m.id) for m in members]
    member_paths = [str(m.path) for m in members]
    member_titles = [m.frontmatter.title or "(untitled)" for m in members]
    return DistillationCandidate(
        cluster_id=cluster_id,
        kind=kind,
        rationale=rationale,
        member_ids=member_ids,
        member_paths=member_paths,
        member_titles=member_titles,
    )


def _cluster_by_topic(vault: Vault, *, min_size: int) -> list[DistillationCandidate]:
    by_topic: dict[str, list[Memory]] = defaultdict(list)
    for memory in vault.walk():
        # Don't fold a distillation back into a topic cluster — they
        # are already a digest.
        if memory.type is MemoryType.DISTILLATION:
            continue
        for topic in _topic_tags(memory):
            by_topic[topic].append(memory)
    out: list[DistillationCandidate] = []
    for topic, members in by_topic.items():
        if len(members) < min_size:
            continue
        out.append(
            _build_candidate(
                cluster_id=topic,
                kind="topic",
                rationale=f"shared {topic!r} tag across {len(members)} memories",
                members=members,
            )
        )
    return out


def _cluster_by_daily_week(vault: Vault, *, min_size: int) -> list[DistillationCandidate]:
    by_week: dict[tuple[str, str], list[Memory]] = defaultdict(list)
    for memory in vault.walk():
        if memory.type is not MemoryType.DAILY:
            continue
        agent = _agent_tag(memory) or "(no-agent)"
        week_id = _iso_week_id(memory.frontmatter.created.date())
        by_week[(agent, week_id)].append(memory)
    out: list[DistillationCandidate] = []
    for (agent, week_id), members in by_week.items():
        if len(members) < min_size:
            continue
        out.append(
            _build_candidate(
                cluster_id=f"daily:{agent}/{week_id}",
                kind="daily-week",
                rationale=(
                    f"{len(members)} type=daily records from agent {agent!r} in week {week_id}"
                ),
                members=members,
            )
        )
    return out


def find_distillation_candidates(
    vault: Vault,
    *,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    skip_already_distilled: bool = True,
) -> list[DistillationCandidate]:
    """Return all distillation candidates above the size threshold.

    The vault is walked once per clustering strategy (the cost is
    bounded and clusters can overlap across strategies — a topic
    cluster and a daily-week cluster may share members). When
    ``skip_already_distilled`` is true (the default), candidates are
    filtered to drop those whose every member is already linked from
    a ``type=distillation`` memory — so re-running the report doesn't
    keep proposing the same already-handled cluster.

    Sorting: candidates are returned newest-first by cluster size, so
    the most actionable clusters surface at the top of the CLI report.
    """
    candidates: list[DistillationCandidate] = []
    candidates.extend(_cluster_by_topic(vault, min_size=min_cluster_size))
    candidates.extend(_cluster_by_daily_week(vault, min_size=min_cluster_size))

    if skip_already_distilled:
        covered = _already_distilled_member_ids(vault)
        if covered:
            kept: list[DistillationCandidate] = []
            for candidate in candidates:
                # Drop a candidate when EVERY member is already
                # distilled — partial coverage still leaves real
                # work to do.
                if all(mid in covered for mid in candidate.member_ids):
                    logger.debug(
                        "distillation: skipping already-distilled cluster %s",
                        candidate.cluster_id,
                    )
                    continue
                kept.append(candidate)
            candidates = kept

    candidates.sort(key=lambda c: c.size, reverse=True)
    return candidates


__all__ = [
    "DEFAULT_MIN_CLUSTER_SIZE",
    "TOPIC_TAG_PREFIX",
    "DistillationCandidate",
    "find_distillation_candidates",
]
