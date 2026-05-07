"""Operator verification report (post-cleanup, post-backfill state check).

Runs read-only against a live vault + index and reports the counts an
operator needs after a hygiene sweep:

- Coverage: total memories, breakdown by type, skill review tickets.
- Derived records: distillations, sessions covered, undistilled-eligible
  sessions remaining (using the same ``is_meaningful_session``
  threshold the writer uses, so the number lines up with what
  ``hygiene distill-sessions --backfill`` would propose).
- Cleanup state: deprecated records, records carrying ``valid_to``,
  active duplicate collision groups still detectable by
  cleanup-retro, and noise hits cleanup-retro would still flag.
- Health: parser/validation skips encountered while walking the vault.

Pure read; no mutation of vault, index, or hygiene cursor. Safe to
run on a production vault — counts come from SQL aggregates plus a
single :class:`Vault.walk` pass that reuses the existing planners.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from memstem.core.index import Index
from memstem.core.storage import Vault
from memstem.hygiene.cleanup_retro import (
    find_dedup_collisions,
    find_noise_hits,
)
from memstem.hygiene.session_distill import (
    DEFAULT_MIN_TURNS,
    DEFAULT_MIN_WORDS,
    find_distilled_session_ids,
    find_session_candidates,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypeCount:
    """Per-type memory counts for the verification report."""

    type: str
    total: int
    deprecated: int
    expired: int


@dataclass(frozen=True)
class VerifyReport:
    """Everything an operator needs to verify post-sweep state.

    All counts are derived from the index + the canonical vault. Every
    field is JSON-serializable so :func:`as_json` can dump the report
    for automation (CI guards, monitoring scrapers).
    """

    vault_path: str
    total_memories: int
    by_type: list[TypeCount]
    deprecated_total: int
    valid_to_total: int
    distilled_session_targets: int
    undistilled_eligible_sessions: int
    active_dedup_groups: int
    active_dedup_skill_groups: int
    active_dedup_to_deprecate: int
    noise_drops: int
    noise_transients: int
    skill_review_tickets: int
    parser_skips: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Render the report as a plain dict suitable for ``json.dumps``."""
        payload = asdict(self)
        # ``by_type`` is a list of dataclass instances after asdict, but
        # the nested asdict already flattens those — explicit no-op here
        # documents the contract.
        return payload


# ─── Helpers ──────────────────────────────────────────────────────────


def _type_counts(index: Index) -> list[TypeCount]:
    """One row per memory ``type``, with deprecated/expired sub-counts.

    ``deprecated`` = ``deprecated_by IS NOT NULL``; ``expired`` = a
    non-null ``valid_to`` (the cell is treated as a "may be expired"
    signal — the search-time filter compares against ``now``, but for
    operator reporting the column existence is what matters).
    """
    rows = index.db.execute(
        """
        SELECT
            type,
            COUNT(*) AS total,
            SUM(CASE WHEN deprecated_by IS NOT NULL THEN 1 ELSE 0 END) AS deprecated,
            SUM(CASE WHEN valid_to IS NOT NULL THEN 1 ELSE 0 END) AS expired
        FROM memories
        GROUP BY type
        ORDER BY total DESC
        """
    ).fetchall()
    return [
        TypeCount(
            type=row["type"] or "unknown",
            total=int(row["total"]),
            deprecated=int(row["deprecated"] or 0),
            expired=int(row["expired"] or 0),
        )
        for row in rows
    ]


def _scalar(index: Index, sql: str) -> int:
    return int(index.db.execute(sql).fetchone()[0])


def _count_skill_review_tickets(vault: Vault) -> int:
    """Count ``skills/_review/*.md`` files (operator artifacts).

    These files are intentionally outside :class:`Vault.walk`'s scope
    (no frontmatter, not memories) — the verification report includes
    them as a workflow-state metric: "how many open tickets is the
    operator carrying?"
    """
    review_dir = vault.root / "skills" / "_review"
    if not review_dir.is_dir():
        return 0
    return sum(1 for p in review_dir.glob("*.md") if p.is_file())


class _SkipCounter(logging.Handler):
    """Capture ``Vault.walk`` skip warnings for the verification report.

    Storage emits one WARNING per file it can't validate. The verify
    pass triggers several walks (one per planner) so the same broken
    file would be reported multiple times; we dedupe by the path token
    in the message so the operator sees one entry per unique file.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._seen: set[str] = set()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name != "memstem.core.storage":
            return
        msg = record.getMessage()
        if not msg.startswith("skipping "):
            return
        # ``skipping <path>: <error>`` — the path is the dedup key.
        rest = msg[len("skipping ") :]
        path_token, _, _ = rest.partition(":")
        path_token = path_token.strip()
        if path_token in self._seen:
            return
        self._seen.add(path_token)
        self.messages.append(msg)


# ─── Public entry point ───────────────────────────────────────────────


def verify_vault(
    vault: Vault,
    index: Index,
    *,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_words: int = DEFAULT_MIN_WORDS,
) -> VerifyReport:
    """Build the verification report for ``vault`` + ``index``.

    Read-only. Reuses the existing planners so any future change to
    "what counts as a duplicate / noise hit / undistilled session"
    flows through automatically.

    ``min_turns`` and ``min_words`` are forwarded to the session
    candidate scan so the report's "undistilled eligible" count
    matches what ``hygiene distill-sessions --backfill`` would
    propose at the same thresholds.
    """
    # 1. Type-broken counts straight from the index.
    by_type = _type_counts(index)
    total_memories = sum(t.total for t in by_type)
    deprecated_total = _scalar(
        index, "SELECT COUNT(*) FROM memories WHERE deprecated_by IS NOT NULL"
    )
    valid_to_total = _scalar(index, "SELECT COUNT(*) FROM memories WHERE valid_to IS NOT NULL")

    # 2. Walk the vault once — capture parser skips while we're at it.
    skip_handler = _SkipCounter()
    storage_logger = logging.getLogger("memstem.core.storage")
    storage_logger.addHandler(skip_handler)
    try:
        # Reuse existing planners. Each does its own walk; we accept the
        # cost because the alternative is duplicating their iteration
        # logic and risking drift.
        distilled = find_distilled_session_ids(vault)
        undistilled_candidates, _ = find_session_candidates(
            vault,
            min_turns=min_turns,
            min_words=min_words,
            recency_days=None,  # backfill mode: full vault, not a window
        )
        dedup_plan = find_dedup_collisions(vault, index)
        noise_plan = find_noise_hits(vault, index)
    finally:
        storage_logger.removeHandler(skip_handler)

    skill_review_tickets = _count_skill_review_tickets(vault)

    return VerifyReport(
        vault_path=str(vault.root),
        total_memories=total_memories,
        by_type=by_type,
        deprecated_total=deprecated_total,
        valid_to_total=valid_to_total,
        distilled_session_targets=len(distilled),
        undistilled_eligible_sessions=len(undistilled_candidates),
        active_dedup_groups=len(dedup_plan.groups),
        active_dedup_skill_groups=len(dedup_plan.skill_groups),
        active_dedup_to_deprecate=dedup_plan.deprecate_count,
        noise_drops=len(noise_plan.drops),
        noise_transients=len(noise_plan.transients),
        skill_review_tickets=skill_review_tickets,
        parser_skips=list(skip_handler.messages),
    )


# ─── Reporting ────────────────────────────────────────────────────────


def format_report(report: VerifyReport, *, parser_skip_sample: int = 5) -> str:
    """Human-readable summary of the verification report.

    ``parser_skip_sample`` caps the inline list of skip messages so a
    pathological vault doesn't drown the operator. The full list lives
    on the report dataclass; ``--json-out`` emits all of them.
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("MEMSTEM VERIFY")
    lines.append("=" * 60)
    lines.append(f"Vault:                    {report.vault_path}")
    lines.append(f"Total memories:           {report.total_memories}")
    lines.append("")
    lines.append("By type:")
    lines.append(f"  {'type':14s}  {'total':>6s}  {'deprecated':>10s}  {'valid_to':>8s}")
    lines.append(f"  {'-' * 50}")
    for t in report.by_type:
        lines.append(f"  {t.type:14s}  {t.total:>6d}  {t.deprecated:>10d}  {t.expired:>8d}")

    lines.append("")
    lines.append("Cleanup state:")
    lines.append(f"  Deprecated records:                   {report.deprecated_total}")
    lines.append(f"  Records with valid_to:                {report.valid_to_total}")
    lines.append(f"  Active dedup collision groups:        {report.active_dedup_groups}")
    lines.append(f"  Active dedup → would deprecate:       {report.active_dedup_to_deprecate}")
    lines.append(f"  Active dedup skill groups (review):   {report.active_dedup_skill_groups}")
    lines.append(f"  Noise drops still detectable:         {report.noise_drops}")
    lines.append(f"  Noise transients still detectable:    {report.noise_transients}")
    lines.append(f"  Skill review tickets open:            {report.skill_review_tickets}")

    lines.append("")
    lines.append("Derived records:")
    lines.append(f"  Sessions covered by distillation:     {report.distilled_session_targets}")
    lines.append(f"  Undistilled eligible sessions left:   {report.undistilled_eligible_sessions}")

    lines.append("")
    if report.parser_skips:
        lines.append(f"Parser/validation skips during scan: {len(report.parser_skips)}")
        for msg in report.parser_skips[:parser_skip_sample]:
            lines.append(f"  · {msg}")
        if len(report.parser_skips) > parser_skip_sample:
            lines.append(f"  · … and {len(report.parser_skips) - parser_skip_sample} more")
    else:
        lines.append("Parser/validation skips during scan: 0")
    return "\n".join(lines)


__all__ = [
    "TypeCount",
    "VerifyReport",
    "format_report",
    "verify_vault",
]
