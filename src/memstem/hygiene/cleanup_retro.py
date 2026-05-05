"""Retroactive cleanup pass for the existing vault (W3, ADR 0012 addendum).

Today's pipeline catches new duplicates and new noise. Brad's live
audit (2026-04-29) found ~20% of the vault is byte-hash duplicate
damage from re-ingestion that pre-dates the Layer 1 dedup PR. This
module applies the *already-shipped* dedup logic and noise rules to
records already in the vault.

Two read-only planners + two writers:

- :func:`find_dedup_collisions` — group memories by normalized body
  hash; size>1 groups are collision candidates.
- :func:`apply_dedup_collisions` — for each non-skill group, mark
  the losers ``deprecated_by: <winner_id>``. Skill groups route to
  ``vault/skills/_review/`` for manual review per ADR 0012.
- :func:`find_noise_hits` — replay the existing noise filter against
  each indexed memory; surface DROP / TAG_TRANSIENT decisions for
  records that pre-date the filter.
- :func:`apply_noise_expiry` — set ``valid_to`` on flagged records
  so default search filters them. Never hard-deletes.

All four are idempotent. Re-running on a clean vault is a no-op.
Every action is reversible because every mutation is a frontmatter
edit on canonical markdown.

Winner selection (per the audit script that prototyped this work):

1. Highest ``importance`` (post-W1 seed).
2. Most retrievals from ``query_log``.
3. Most-recently-updated.
4. Lexicographically smallest ``id`` (deterministic tiebreak).

Audit logging:

Every applied verdict appends a row to ``dedup_audit`` with
``judge="layer1-retro"`` so the operator can later trace which
pairs were collapsed and recover from a wrong call.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from memstem.adapters.base import MemoryRecord
from memstem.core.dedup import (
    increment_seen_count,
    normalized_body_hash,
    record_body_hash,
)
from memstem.core.extraction import (
    NoiseAction,
    NoiseDecision,
    noise_filter,
)
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault

logger = logging.getLogger(__name__)


# ─── Data classes ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CollisionMember:
    """One memory in a body-hash collision group."""

    id: str
    type: str
    title: str | None
    path: str
    importance: float | None
    retrievals: int
    updated: datetime | None


@dataclass(frozen=True, slots=True)
class CollisionGroup:
    """A set of memories sharing the same normalized body hash."""

    body_hash: str
    members: tuple[CollisionMember, ...]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def involves_skill(self) -> bool:
        return any(m.type == "skill" for m in self.members)


@dataclass(frozen=True, slots=True)
class CollisionWinner:
    """The chosen winner of a collision group, plus the losers."""

    winner: CollisionMember
    losers: tuple[CollisionMember, ...]
    coin_flip: bool
    """True when winner was chosen on the deterministic id tiebreak —
    importance / retrievals / updated all matched."""


@dataclass(frozen=True, slots=True)
class DedupPlan:
    """The full retro-dedup plan, ready for ``--apply``."""

    groups: tuple[CollisionGroup, ...]
    winners: tuple[CollisionWinner, ...]
    """Same order as ``groups``."""

    @property
    def total_records(self) -> int:
        return sum(g.size for g in self.groups)

    @property
    def deprecate_count(self) -> int:
        return sum(len(w.losers) for w in self.winners)

    @property
    def skill_groups(self) -> list[CollisionGroup]:
        return [g for g in self.groups if g.involves_skill]


@dataclass(frozen=True, slots=True)
class NoiseHit:
    """A memory that matches a noise rule retroactively."""

    id: str
    path: str
    title: str | None
    decision: NoiseDecision


@dataclass(frozen=True, slots=True)
class NoisePlan:
    """The full retro-noise plan."""

    drops: tuple[NoiseHit, ...]
    transients: tuple[NoiseHit, ...]

    @property
    def total(self) -> int:
        return len(self.drops) + len(self.transients)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of an apply pass."""

    deprecated: int = 0
    expired: int = 0
    skill_review_tickets: int = 0
    audit_rows: int = 0
    apply_errors: list[str] = field(default_factory=list)


# ─── Planners (read-only) ────────────────────────────────────────────


def _retrievals_by_id(db: sqlite3.Connection) -> dict[str, int]:
    """Count query_log rows per memory_id (proxy for "is this id in active use?")."""
    rows = db.execute(
        "SELECT memory_id, COUNT(*) AS cnt FROM query_log GROUP BY memory_id"
    ).fetchall()
    return {r["memory_id"]: int(r["cnt"]) for r in rows}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_dedup_collisions(vault: Vault, index: Index) -> DedupPlan:
    """Walk the vault, group by normalized body hash, return the plan.

    Read-only. Skips records whose body can't be read or whose path
    is missing — the goal is best-effort, not a complete inventory.
    """
    rows = index.db.execute(
        """
        SELECT id, type, source, title, path, created, updated, importance,
               deprecated_by
        FROM memories
        """
    ).fetchall()
    retrievals = _retrievals_by_id(index.db)

    by_hash: dict[str, list[CollisionMember]] = defaultdict(list)
    for row in rows:
        # Records already deprecated by an earlier pass are excluded from
        # the planner — they're already collapsed; revisiting them only
        # produces noise in the audit log.
        if row["deprecated_by"]:
            continue
        try:
            memory = vault.read(row["path"])
        except Exception as exc:
            logger.debug("cleanup_retro: skipping %s: %s", row["id"], exc)
            continue
        h = normalized_body_hash(memory.body)
        by_hash[h].append(
            CollisionMember(
                id=row["id"],
                type=row["type"] or "memory",
                title=row["title"],
                path=row["path"],
                importance=row["importance"],
                retrievals=retrievals.get(row["id"], 0),
                updated=_parse_iso(row["updated"]),
            )
        )

    groups: list[CollisionGroup] = []
    winners: list[CollisionWinner] = []
    for h, members in by_hash.items():
        if len(members) < 2:
            continue
        group = CollisionGroup(body_hash=h, members=tuple(members))
        winner = select_winner(group)
        groups.append(group)
        winners.append(winner)

    # Stable order: largest groups first; within a tier, by winner id.
    sorted_pairs = sorted(
        zip(groups, winners, strict=True), key=lambda p: (-p[0].size, p[1].winner.id)
    )
    groups_sorted = tuple(g for g, _ in sorted_pairs)
    winners_sorted = tuple(w for _, w in sorted_pairs)
    return DedupPlan(groups=groups_sorted, winners=winners_sorted)


def select_winner(group: CollisionGroup) -> CollisionWinner:
    """Pick the winner per the audit heuristic.

    Tiebreak order (highest is winner):
    1. ``importance`` (None treated as the neutral 0.5 default).
    2. ``retrievals`` from ``query_log``.
    3. ``updated`` timestamp (newer wins).
    4. Lexicographically smallest ``id`` (deterministic).

    Sets ``coin_flip = True`` when none of (1)-(3) distinguish the
    winner from any loser — the tiebreak fell through to id ordering.
    """

    def key(m: CollisionMember) -> tuple[float, int, float, str]:
        importance = m.importance if m.importance is not None else 0.5
        updated_ts = m.updated.timestamp() if m.updated is not None else 0.0
        return (-importance, -m.retrievals, -updated_ts, m.id)

    sorted_members = sorted(group.members, key=key)
    winner = sorted_members[0]
    losers = tuple(sorted_members[1:])
    # Coin-flip detection: every loser has the same (importance,
    # retrievals, updated) signal as the winner — only id differs.
    winner_signal = key(winner)[:3]
    coin_flip = all(key(loser)[:3] == winner_signal for loser in losers) if losers else False
    return CollisionWinner(winner=winner, losers=losers, coin_flip=coin_flip)


def find_noise_hits(
    vault: Vault,
    index: Index,
    boot_echo_hashes: frozenset[str] | None = None,
) -> NoisePlan:
    """Run the noise filter against every indexed memory; surface hits.

    Records already expired (``valid_to`` past) are skipped — the
    filter would fire again, but they're already invisible to default
    search.
    """
    rows = index.db.execute(
        """
        SELECT id, source, title, path, valid_to, deprecated_by
        FROM memories
        """
    ).fetchall()

    drops: list[NoiseHit] = []
    transients: list[NoiseHit] = []
    now = datetime.now(tz=UTC)
    for row in rows:
        if row["deprecated_by"]:
            continue
        valid_to_ts = _parse_iso(row["valid_to"])
        if valid_to_ts is not None and valid_to_ts <= now:
            continue
        try:
            memory = vault.read(row["path"])
        except Exception as exc:
            logger.debug("cleanup_retro: skipping %s: %s", row["id"], exc)
            continue
        # Build a synthetic MemoryRecord just to feed the noise filter.
        synthetic = MemoryRecord(
            source=row["source"] or "unknown",
            ref=row["path"],
            title=row["title"],
            body=memory.body,
            tags=[],
            metadata={},
        )
        decision = noise_filter(synthetic, boot_echo_hashes=boot_echo_hashes)
        if decision.action is NoiseAction.KEEP:
            continue
        hit = NoiseHit(
            id=row["id"],
            path=row["path"],
            title=row["title"],
            decision=decision,
        )
        if decision.action is NoiseAction.DROP:
            drops.append(hit)
        else:
            transients.append(hit)

    return NoisePlan(drops=tuple(drops), transients=tuple(transients))


# ─── Writers ─────────────────────────────────────────────────────────


SKILL_REVIEW_DIRNAME = "skills/_review"


def write_skill_review_ticket(
    vault: Vault,
    group: CollisionGroup,
    winner: CollisionWinner,
    *,
    now: datetime | None = None,
) -> Path:
    """Write a markdown review ticket for a skill collision.

    Per ADR 0012, skill collisions never auto-merge — they require
    operator review. The ticket contains both candidates' titles +
    paths so the operator can compare without a separate vault walk.
    """
    moment = now or datetime.now(tz=UTC)
    iso = moment.strftime("%Y%m%dT%H%M%SZ")
    slug_source = winner.winner.title or winner.winner.id
    slug = "".join(c if c.isalnum() else "-" for c in slug_source.lower())[:40].strip("-")
    ticket_path = Path(f"{SKILL_REVIEW_DIRNAME}/{iso}-{slug or 'skill'}.md")

    body_lines = [
        "# Skill collision review ticket",
        "",
        f"Generated: {moment.isoformat()}",
        f"Body hash: `{group.body_hash}`",
        "",
        "## Candidates",
        "",
    ]
    for i, member in enumerate(group.members):
        marker = "WINNER (proposed)" if member.id == winner.winner.id else "loser (proposed)"
        body_lines.append(f"### {marker}: {member.title or '(no title)'}")
        body_lines.append("")
        body_lines.append(f"- id: `{member.id}`")
        body_lines.append(f"- path: `{member.path}`")
        body_lines.append(f"- type: `{member.type}`")
        body_lines.append(f"- importance: {member.importance}")
        body_lines.append(f"- retrievals: {member.retrievals}")
        body_lines.append(f"- updated: {member.updated.isoformat() if member.updated else '-'}")
        body_lines.append("")
        if i < len(group.members) - 1:
            body_lines.append("---")
            body_lines.append("")
    body_lines.extend(
        [
            "## Resolution options",
            "",
            "- **Keep all** if the skills are intentionally similar (not a duplicate). "
            "Delete this ticket file to clear it from the queue.",
            "- **Merge** by editing the winner skill in place, deleting the loser "
            "skill files manually, then deleting this ticket file.",
            "- **Dismiss** by deleting this ticket file. The collision groups "
            "stay in `cleanup-retro` dry-run output until the underlying "
            "skill bodies actually diverge.",
            "",
            "Tracked under ADR 0012. A first-class `memstem skill-review` CLI "
            "is on the roadmap but not yet shipped — the current workflow is "
            "manual edits + manual ticket deletion.",
            "",
        ]
    )

    full_path = vault.root / ticket_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("\n".join(body_lines), encoding="utf-8")
    return ticket_path


def apply_dedup_collisions(
    vault: Vault,
    index: Index,
    plan: DedupPlan,
    *,
    skip_skill_groups: bool = True,
) -> ApplyResult:
    """Mutate the vault: write ``deprecated_by`` to losers; ticket skills.

    Args:
        vault: Canonical markdown store.
        index: SQLite index. Audit rows go to ``dedup_audit``.
        plan: The result of :func:`find_dedup_collisions`.
        skip_skill_groups: When True (the default), skill-involved
            groups never auto-merge — a review ticket is written
            instead. Set False only in tests or for an operator who
            *explicitly* wants to auto-apply (not recommended).

    Returns:
        :class:`ApplyResult` with counts.
    """
    deprecated = 0
    skill_tickets = 0
    audit_rows = 0
    errors: list[str] = []
    audit_ts = datetime.now(tz=UTC).isoformat()

    for group, winner in zip(plan.groups, plan.winners, strict=True):
        if group.involves_skill and skip_skill_groups:
            try:
                ticket = write_skill_review_ticket(vault, group, winner)
                logger.info("cleanup_retro: skill review ticket %s", ticket)
                skill_tickets += 1
            except Exception as exc:
                errors.append(f"skill ticket write failed for {winner.winner.id}: {exc}")
            continue

        for loser in winner.losers:
            try:
                memory = vault.read(loser.path)
            except Exception as exc:
                errors.append(f"cannot read {loser.path}: {exc}")
                continue
            try:
                from uuid import UUID

                new_fm = memory.frontmatter.model_copy(
                    update={"deprecated_by": UUID(winner.winner.id)}
                )
                new_memory = Memory(frontmatter=new_fm, body=memory.body, path=memory.path)
                vault.write(new_memory)
                index.upsert(new_memory)
                deprecated += 1
            except Exception as exc:
                errors.append(f"cannot deprecate {loser.id}: {exc}")
                continue

            # body_hash_index housekeeping: bump seen_count on the
            # surviving record's hash; drop the loser's row if the
            # current hash mapping points at the loser.
            try:
                with index._lock, index.db:
                    # Ensure the hash points to the winner; the loser's
                    # mapping (if any) for the same hash is collapsed
                    # by record_body_hash.
                    record_body_hash(index.db, group.body_hash, winner.winner.id)
                    increment_seen_count(index.db, group.body_hash)
                    # Audit row.
                    index.db.execute(
                        """
                        INSERT INTO dedup_audit
                            (ts, new_id, existing_id, verdict, rationale, judge, applied)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            audit_ts,
                            loser.id,
                            winner.winner.id,
                            "DUPLICATE",
                            "retro layer-1 (body-hash collision)",
                            "layer1-retro",
                        ),
                    )
                    audit_rows += 1
            except Exception as exc:
                errors.append(f"audit/hash write failed for {loser.id}: {exc}")

    return ApplyResult(
        deprecated=deprecated,
        skill_review_tickets=skill_tickets,
        audit_rows=audit_rows,
        apply_errors=errors,
    )


def apply_noise_expiry(
    vault: Vault,
    index: Index,
    plan: NoisePlan,
    *,
    drop_ttl_days: int = 0,
    transient_ttl_days: int | None = None,
) -> ApplyResult:
    """Mark noise hits with ``valid_to``.

    For DROP hits, sets ``valid_to = now + drop_ttl_days`` (default
    ``0`` — immediate soft-delete; record stays on disk but search
    filters it). For TAG_TRANSIENT hits, uses the decision's own
    ``ttl_days`` unless overridden via ``transient_ttl_days``.
    """
    expired = 0
    errors: list[str] = []
    now = datetime.now(tz=UTC)

    for hit in plan.drops:
        try:
            memory = vault.read(hit.path)
        except Exception as exc:
            errors.append(f"cannot read {hit.path}: {exc}")
            continue
        from datetime import timedelta

        valid_to = now + timedelta(days=drop_ttl_days)
        try:
            new_fm = memory.frontmatter.model_copy(update={"valid_to": valid_to})
            new_memory = Memory(frontmatter=new_fm, body=memory.body, path=memory.path)
            vault.write(new_memory)
            index.upsert(new_memory)
            expired += 1
        except Exception as exc:
            errors.append(f"cannot expire {hit.id}: {exc}")

    for hit in plan.transients:
        try:
            memory = vault.read(hit.path)
        except Exception as exc:
            errors.append(f"cannot read {hit.path}: {exc}")
            continue
        from datetime import timedelta

        ttl = (
            transient_ttl_days if transient_ttl_days is not None else (hit.decision.ttl_days or 28)
        )
        valid_to = now + timedelta(days=ttl)
        try:
            new_fm = memory.frontmatter.model_copy(update={"valid_to": valid_to})
            new_memory = Memory(frontmatter=new_fm, body=memory.body, path=memory.path)
            vault.write(new_memory)
            index.upsert(new_memory)
            expired += 1
        except Exception as exc:
            errors.append(f"cannot expire {hit.id}: {exc}")

    return ApplyResult(expired=expired, apply_errors=errors)


# ─── Reporting ───────────────────────────────────────────────────────


def format_dedup_report(plan: DedupPlan) -> str:
    """Human-readable summary of the dedup plan (used by --dry-run output)."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("RETRO DEDUP PLAN")
    lines.append("=" * 60)
    lines.append(f"Collision groups:        {len(plan.groups)}")
    lines.append(f"Records to deprecate:    {plan.deprecate_count}")
    skill_groups = plan.skill_groups
    lines.append(f"Skill-involved groups:   {len(skill_groups)} (route to review queue)")
    coin_flips = sum(1 for w in plan.winners if w.coin_flip)
    lines.append(f"Coin-flip groups:        {coin_flips}")
    lines.append("")
    if plan.groups:
        lines.append("Top 15 collision groups by size:")
        lines.append(
            f"  {'size':>4s}  {'flags':5s}  {'type':10s}  {'rtv':>4s}  {'winner_title':40s}"
        )
        lines.append(f"  {'-' * 70}")
        for g, w in zip(plan.groups[:15], plan.winners[:15], strict=True):
            flags = ("S" if g.involves_skill else "-") + ("C" if w.coin_flip else "-")
            title = (w.winner.title or "(no title)")[:40]
            lines.append(
                f"  {g.size:>4d}  {flags:5s}  {w.winner.type:10s}  "
                f"{w.winner.retrievals:>4d}  {title}"
            )
    return "\n".join(lines)


def format_noise_report(plan: NoisePlan) -> str:
    """Human-readable summary of the noise-replay plan."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("RETRO NOISE PLAN")
    lines.append("=" * 60)
    lines.append(f"Records to drop (soft-delete):     {len(plan.drops)}")
    lines.append(f"Records to TTL (expire after N d): {len(plan.transients)}")
    lines.append("")
    if plan.drops:
        lines.append("Drop kinds:")
        kinds: dict[str, int] = defaultdict(int)
        for h in plan.drops:
            kinds[h.decision.kind or "?"] += 1
        for kind, n in sorted(kinds.items(), key=lambda x: -x[1]):
            lines.append(f"  {kind:20s} {n:>5d}")
    if plan.transients:
        lines.append("")
        lines.append("TTL kinds:")
        kinds = defaultdict(int)
        for h in plan.transients:
            kinds[h.decision.kind or "?"] += 1
        for kind, n in sorted(kinds.items(), key=lambda x: -x[1]):
            lines.append(f"  {kind:20s} {n:>5d}")
    return "\n".join(lines)


__all__ = [
    "SKILL_REVIEW_DIRNAME",
    "ApplyResult",
    "CollisionGroup",
    "CollisionMember",
    "CollisionWinner",
    "DedupPlan",
    "NoiseHit",
    "NoisePlan",
    "apply_dedup_collisions",
    "apply_noise_expiry",
    "find_dedup_collisions",
    "find_noise_hits",
    "format_dedup_report",
    "format_noise_report",
    "select_winner",
    "write_skill_review_ticket",
]
