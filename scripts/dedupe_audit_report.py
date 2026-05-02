#!/usr/bin/env python3
"""Read-only multi-class dedupe audit for the MemStem vault.

This script is deliberately mutation-free. It reads the vault's
canonical markdown and the SQLite index, classifies candidate
duplicate groups into the classes defined in
``docs/dedupe-audit.md``, and writes two artifacts:

- A markdown summary report (human-readable).
- A JSON report (machine-readable; same data plus full member lists).

It does **not** import or call any vault writer (no
``Vault.write``, no ``Index.upsert``, no ``apply_*`` from
``hygiene.cleanup_retro``). Running it cannot mutate state.

Usage:

    python3 scripts/dedupe_audit_report.py [--vault PATH] [--out-dir PATH]

Default vault path resolution mirrors the CLI: ``--vault`` argument >
``MEMSTEM_VAULT`` env var > ``~/memstem-vault``. Default output
directory is ``<vault>/_meta/audits/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Importing only the read-only halves of cleanup_retro. The writer
# functions (apply_dedup_collisions, apply_noise_expiry,
# write_skill_review_ticket) are intentionally NOT imported — keeps
# the dependency graph honest about this script's read-only nature.
from memstem.core.dedup import normalized_body_hash
from memstem.core.frontmatter import MemoryType
from memstem.core.index import Index
from memstem.core.storage import Vault
from memstem.hygiene.cleanup_retro import (
    CollisionGroup,
    CollisionMember,
    select_winner,
)

DEFAULT_VAULT = Path.home() / "memstem-vault"


# ─── Data classes ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RecordView:
    """Frozen snapshot of one record for audit purposes."""

    id: str
    type: str
    source: str | None
    title: str | None
    path: str
    body_hash: str
    provenance_source: str | None
    provenance_ref: str | None
    provenance_ingested_at: str | None
    created: str | None
    updated: str | None
    importance: float | None
    retrievals: int
    deprecated_by: str | None
    has_links: bool
    tag_count: int


@dataclass(slots=True)
class CandidateGroup:
    """One candidate-duplicate group, in one class."""

    klass: str
    confidence: str
    risk: str
    rationale: str
    recommended_action: str
    members: list[RecordView]
    winner_id: str | None = None
    coin_flip: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClassSummary:
    klass: str
    title: str
    description: str
    confidence: str
    risk: str
    recommended_action: str
    group_count: int = 0
    record_count: int = 0


# ─── Class catalog (matches docs/dedupe-audit.md) ────────────────────

CLASS_CATALOG: list[ClassSummary] = [
    ClassSummary(
        klass="A",
        title="Exact body-hash collision (low-risk types)",
        description=(
            "Same normalized body hash, same type, type ∈ {memory, daily, "
            "session}. Prototypical reingest signature."
        ),
        confidence="high",
        risk="low",
        recommended_action="safe_quarantine_candidate",
    ),
    ClassSummary(
        klass="B",
        title="Reingest of the same source ref",
        description=(
            "Same provenance.ref AND same body hash. A subset of Class A "
            "with confirming source-pointer match."
        ),
        confidence="high",
        risk="low",
        recommended_action="safe_quarantine_candidate",
    ),
    ClassSummary(
        klass="C",
        title="Source-updated (NOT a duplicate)",
        description=(
            "Same provenance.ref but different body hashes. Source file "
            "was edited between ingests; both versions kept on disk."
        ),
        confidence="high",
        risk="high",
        recommended_action="keep_all",
    ),
    ClassSummary(
        klass="D",
        title="Title-equivalent + body-hash equal",
        description=(
            "Same body hash AND same normalized title. Strongest "
            "confirming signal short of provenance match."
        ),
        confidence="very_high",
        risk="low",
        recommended_action="safe_quarantine_candidate",
    ),
    ClassSummary(
        klass="E",
        title="Title-equivalent + body-hash different",
        description=(
            "Same title but different bodies. Could be source-updated, "
            "near-duplicate, or unrelated."
        ),
        confidence="low",
        risk="high",
        recommended_action="manual_review",
    ),
    ClassSummary(
        klass="F",
        title="Cross-type body-hash collision",
        description=(
            "Same body hash but different record types (e.g. memory + "
            "distillation). Bodies match but roles differ."
        ),
        confidence="mixed",
        risk="high",
        recommended_action="manual_review",
    ),
    ClassSummary(
        klass="G",
        title="Skill body-hash collision",
        description=(
            "Body-hash collision involving any record with type:skill. "
            "Per ADR 0012, never auto-merged."
        ),
        confidence="high",
        risk="very_high",
        recommended_action="manual_review",
    ),
    ClassSummary(
        klass="H",
        title="Derived-record collision (project / distillation)",
        description=(
            "Body-hash collision between records of type project or "
            "distillation. Curated rollups; collisions deserve scrutiny."
        ),
        confidence="mixed",
        risk="high",
        recommended_action="manual_review",
    ),
]

CLASS_BY_KEY = {c.klass: c for c in CLASS_CATALOG}


LOW_RISK_TYPES = {
    MemoryType.MEMORY.value,
    MemoryType.DAILY.value,
    MemoryType.SESSION.value,
}
DERIVED_TYPES = {
    MemoryType.DISTILLATION.value,
    MemoryType.PROJECT.value,
}


# ─── Read-only helpers ───────────────────────────────────────────────


def _resolve_vault(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("MEMSTEM_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_VAULT


def _retrievals_by_id(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        "SELECT memory_id, COUNT(*) AS cnt FROM query_log GROUP BY memory_id"
    ).fetchall()
    return {r["memory_id"]: int(r["cnt"]) for r in rows}


def _normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    return " ".join(title.lower().split())


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_views(vault: Vault, index: Index) -> list[RecordView]:
    """Walk every indexed record and return a frozen view per file.

    Records whose markdown can't be read or parsed are skipped with a
    warning (best-effort audit, not a complete inventory).
    """
    rows = index.db.execute(
        """
        SELECT id, type, source, title, path, created, updated, importance,
               deprecated_by
        FROM memories
        """
    ).fetchall()
    retrievals = _retrievals_by_id(index.db)

    views: list[RecordView] = []
    for row in rows:
        try:
            memory = vault.read(row["path"])
        except Exception as exc:
            print(
                f"  warn: skipping {row['id']} ({row['path']}): {exc}",
                file=sys.stderr,
            )
            continue
        fm = memory.frontmatter
        prov = fm.provenance
        views.append(
            RecordView(
                id=row["id"],
                type=row["type"] or "memory",
                source=row["source"],
                title=row["title"],
                path=row["path"],
                body_hash=normalized_body_hash(memory.body),
                provenance_source=prov.source if prov else None,
                provenance_ref=prov.ref if prov else None,
                provenance_ingested_at=(
                    prov.ingested_at.isoformat() if prov and prov.ingested_at else None
                ),
                created=row["created"],
                updated=row["updated"],
                importance=row["importance"],
                retrievals=retrievals.get(row["id"], 0),
                deprecated_by=row["deprecated_by"],
                has_links=bool(fm.links),
                tag_count=len(fm.tags),
            )
        )
    return views


# ─── Classification ──────────────────────────────────────────────────


def _to_collision_member(view: RecordView) -> CollisionMember:
    return CollisionMember(
        id=view.id,
        type=view.type,
        title=view.title,
        path=view.path,
        importance=view.importance,
        retrievals=view.retrievals,
        updated=_parse_iso(view.updated),
    )


def _winner_for(views: list[RecordView]) -> tuple[str, bool]:
    """Run cleanup_retro's winner heuristic over a candidate group."""
    members = tuple(_to_collision_member(v) for v in views)
    pseudo = CollisionGroup(body_hash="", members=members)
    chosen = select_winner(pseudo)
    return chosen.winner.id, chosen.coin_flip


def classify(views: list[RecordView]) -> list[CandidateGroup]:
    """Produce the full list of candidate groups across all classes.

    A single record can appear in multiple groups (e.g. it can be in
    both Class A and Class B if its body-hash collision also shares a
    provenance.ref). That's intentional — each class describes a
    different signal and the operator may want to filter by class.
    """
    active = [v for v in views if not v.deprecated_by]
    groups: list[CandidateGroup] = []

    # Pre-bucket by body hash for classes A, D, F, G, H.
    by_hash: dict[str, list[RecordView]] = defaultdict(list)
    for v in active:
        by_hash[v.body_hash].append(v)

    for _h, members in by_hash.items():
        if len(members) < 2:
            continue
        types = {m.type for m in members}
        has_skill = MemoryType.SKILL.value in types
        cross_type = len(types) > 1

        # Class G — skill anywhere → high risk override.
        if has_skill:
            groups.append(
                _make_group(
                    "G",
                    members,
                    rationale=(
                        f"body-hash collision involving type:skill across "
                        f"{len(members)} record(s); types={sorted(types)}"
                    ),
                )
            )
            continue

        # Class F — cross-type collision (no skills).
        if cross_type:
            groups.append(
                _make_group(
                    "F",
                    members,
                    rationale=(f"body-hash collision across {len(types)} types: {sorted(types)}"),
                )
            )
            continue

        # At this point all members share a single non-skill type.
        only_type = next(iter(types))

        # Class H — derived (project/distillation).
        if only_type in DERIVED_TYPES:
            groups.append(
                _make_group(
                    "H",
                    members,
                    rationale=(
                        f"body-hash collision among curated {only_type} "
                        f"records ({len(members)} members)"
                    ),
                )
            )
            continue

        # Class A — exact body-hash collision, low-risk type.
        if only_type in LOW_RISK_TYPES:
            normalized_titles = {_normalize_title(m.title) for m in members}
            confirming_title = len(normalized_titles) == 1 and next(iter(normalized_titles))
            groups.append(
                _make_group(
                    "A",
                    members,
                    rationale=(f"same body hash among {len(members)} {only_type} record(s)"),
                )
            )
            # Class D — confirming title equality.
            if confirming_title:
                groups.append(
                    _make_group(
                        "D",
                        members,
                        rationale=(
                            f"body hash AND normalized title both equal ({len(members)} record(s))"
                        ),
                    )
                )
        else:
            # Unknown type bucket — surface in F as a defensive measure.
            groups.append(
                _make_group(
                    "F",
                    members,
                    rationale=(
                        f"body-hash collision in unmapped type '{only_type}' "
                        f"({len(members)} record(s))"
                    ),
                )
            )

    # Class B — same provenance.ref + same body hash.
    by_ref_hash: dict[tuple[str, str], list[RecordView]] = defaultdict(list)
    for v in active:
        if v.provenance_ref:
            by_ref_hash[(v.provenance_ref, v.body_hash)].append(v)
    for (ref, _h), members in by_ref_hash.items():
        if len(members) < 2:
            continue
        groups.append(
            _make_group(
                "B",
                members,
                rationale=(
                    f"{len(members)} record(s) with provenance.ref={ref} and identical body hash"
                ),
                extra={"provenance_ref": ref},
            )
        )

    # Class C — same provenance.ref + DIFFERENT body hashes.
    by_ref: dict[str, list[RecordView]] = defaultdict(list)
    for v in active:
        if v.provenance_ref:
            by_ref[v.provenance_ref].append(v)
    for ref, members in by_ref.items():
        if len(members) < 2:
            continue
        if len({m.body_hash for m in members}) < 2:
            continue
        groups.append(
            _make_group(
                "C",
                members,
                rationale=(
                    f"{len(members)} record(s) share provenance.ref={ref} "
                    "but bodies differ — source-updated, NOT a duplicate"
                ),
                extra={"provenance_ref": ref},
            )
        )

    # Class E — same normalized title + DIFFERENT body hashes.
    by_title: dict[str, list[RecordView]] = defaultdict(list)
    for v in active:
        nt = _normalize_title(v.title)
        if nt:
            by_title[nt].append(v)
    for title, members in by_title.items():
        if len(members) < 2:
            continue
        if len({m.body_hash for m in members}) < 2:
            continue
        # Skip pure date-bucketed daily titles ("2026-04-15 (Wednesday)") —
        # these are intentionally one-per-day and shouldn't dominate the
        # report. They show up in Class A/D when bodies do match.
        if all(m.type == MemoryType.DAILY.value for m in members):
            continue
        groups.append(
            _make_group(
                "E",
                members,
                rationale=(
                    f"{len(members)} record(s) share normalized title "
                    f"'{title[:60]}' but bodies differ"
                ),
                extra={"normalized_title": title},
            )
        )

    return groups


def _make_group(
    klass: str,
    members: list[RecordView],
    *,
    rationale: str,
    extra: dict[str, Any] | None = None,
) -> CandidateGroup:
    catalog = CLASS_BY_KEY[klass]
    winner_id: str | None = None
    coin_flip = False
    if catalog.recommended_action == "safe_quarantine_candidate":
        winner_id, coin_flip = _winner_for(members)
    return CandidateGroup(
        klass=klass,
        confidence=catalog.confidence,
        risk=catalog.risk,
        rationale=rationale,
        recommended_action=catalog.recommended_action,
        members=list(members),
        winner_id=winner_id,
        coin_flip=coin_flip,
        extra=dict(extra or {}),
    )


# ─── Reporting ───────────────────────────────────────────────────────


def _summary_row(klass: str, groups: list[CandidateGroup]) -> dict[str, Any]:
    catalog = CLASS_BY_KEY[klass]
    in_class = [g for g in groups if g.klass == klass]
    return {
        "class": klass,
        "title": catalog.title,
        "groups": len(in_class),
        "records": sum(len(g.members) for g in in_class),
        "confidence": catalog.confidence,
        "risk": catalog.risk,
        "recommended_action": catalog.recommended_action,
    }


def render_markdown(
    *,
    vault_path: Path,
    views: list[RecordView],
    groups: list[CandidateGroup],
    generated: datetime,
    top_n_per_class: int = 8,
) -> str:
    type_counts = Counter(v.type for v in views)
    deprecated_count = sum(1 for v in views if v.deprecated_by)
    lines: list[str] = []
    lines.append("# MemStem dedupe audit report")
    lines.append("")
    lines.append(f"- Generated: `{generated.isoformat()}`")
    lines.append(f"- Vault: `{vault_path}`")
    lines.append(f"- Total records inspected: **{len(views)}**")
    lines.append("- By type: " + ", ".join(f"{t}={n}" for t, n in sorted(type_counts.items())))
    lines.append(f"- Already deprecated (excluded from groups): {deprecated_count}")
    lines.append("")
    lines.append("> Read-only audit. No file or index has been mutated.")
    lines.append("> See [`docs/dedupe-audit.md`](dedupe-audit.md) for the policy.")
    lines.append("")

    lines.append("## Summary by class")
    lines.append("")
    lines.append("| Class | Title | Groups | Records | Confidence | Risk | Action |")
    lines.append("|-------|-------|--------|---------|------------|------|--------|")
    for klass in CLASS_BY_KEY:
        summary = _summary_row(klass, groups)
        lines.append(
            f"| {klass} | {summary['title']} | {summary['groups']} | "
            f"{summary['records']} | {summary['confidence']} | "
            f"{summary['risk']} | `{summary['recommended_action']}` |"
        )
    lines.append("")

    # Per-class detail.
    for klass, catalog in CLASS_BY_KEY.items():
        in_class = [g for g in groups if g.klass == klass]
        if not in_class:
            continue
        lines.append(f"## Class {klass} — {catalog.title}")
        lines.append("")
        lines.append(catalog.description)
        lines.append("")
        lines.append(
            f"- Confidence: **{catalog.confidence}**"
            f"  ·  Risk: **{catalog.risk}**"
            f"  ·  Action: `{catalog.recommended_action}`"
        )
        lines.append(f"- Groups in this class: **{len(in_class)}**")
        lines.append(f"- Records affected: **{sum(len(g.members) for g in in_class)}**")
        lines.append("")
        sorted_groups = sorted(
            in_class,
            key=lambda g: (-len(g.members), g.members[0].id),
        )
        shown = sorted_groups[:top_n_per_class]
        if len(sorted_groups) > len(shown):
            lines.append(
                f"_Showing top {len(shown)} of {len(sorted_groups)} groups by "
                "size; full list in JSON report._"
            )
            lines.append("")
        for i, group in enumerate(shown, start=1):
            lines.append(f"### Class {klass} group {i} — {len(group.members)} record(s)")
            lines.append("")
            lines.append(f"- Rationale: {group.rationale}")
            if group.extra:
                for k, val in group.extra.items():
                    lines.append(f"- {k}: `{val}`")
            if group.winner_id:
                marker = " (coin-flip)" if group.coin_flip else ""
                lines.append(f"- Recommended winner: `{group.winner_id}`{marker}")
            lines.append(f"- Recommended action: `{group.recommended_action}`")
            lines.append("")
            lines.append(
                "| role | id | type | source | title | path | "
                "provenance.ref | ingested_at | importance | retrievals |"
            )
            lines.append(
                "|------|----|------|--------|-------|------|----------------|"
                "-------------|------------|-----------|"
            )
            for m in group.members:
                role = (
                    "winner"
                    if group.winner_id and m.id == group.winner_id
                    else "loser"
                    if group.winner_id
                    else "—"
                )
                title = (m.title or "—").replace("|", "\\|")[:60]
                ref = (m.provenance_ref or "—").replace("|", "\\|")[:60]
                ingested = (m.provenance_ingested_at or "—")[:19]
                imp = f"{m.importance:.2f}" if m.importance is not None else "—"
                lines.append(
                    f"| {role} | `{m.id}` | {m.type} | {m.source or '—'} | "
                    f"{title} | `{m.path}` | {ref} | {ingested} | "
                    f"{imp} | {m.retrievals} |"
                )
            lines.append("")

    if not any(g.klass for g in groups):
        lines.append("## Result")
        lines.append("")
        lines.append("No candidate duplicate groups detected. Vault is clean.")
        lines.append("")

    return "\n".join(lines)


def render_json(
    *,
    vault_path: Path,
    views: list[RecordView],
    groups: list[CandidateGroup],
    generated: datetime,
) -> str:
    type_counts = Counter(v.type for v in views)
    deprecated_count = sum(1 for v in views if v.deprecated_by)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": generated.isoformat(),
        "vault": str(vault_path),
        "totals": {
            "records": len(views),
            "by_type": dict(sorted(type_counts.items())),
            "deprecated": deprecated_count,
        },
        "classes": [{**asdict(c), **_summary_row(c.klass, groups)} for c in CLASS_CATALOG],
        "groups": [
            {
                "class": g.klass,
                "confidence": g.confidence,
                "risk": g.risk,
                "rationale": g.rationale,
                "recommended_action": g.recommended_action,
                "winner_id": g.winner_id,
                "coin_flip": g.coin_flip,
                "extra": g.extra,
                "members": [asdict(m) for m in g.members],
            }
            for g in groups
        ],
    }
    return json.dumps(payload, indent=2, default=str)


# ─── Entry point ─────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only multi-class dedupe audit. Produces a markdown "
            "summary and a JSON report. Mutates nothing."
        ),
    )
    parser.add_argument(
        "--vault",
        help="Vault path (default: $MEMSTEM_VAULT or ~/memstem-vault).",
    )
    parser.add_argument(
        "--out-dir",
        help="Output directory (default: <vault>/_meta/audits/).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=8,
        help="Top N groups per class to show in the markdown report.",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="Print the summary table to stdout in addition to writing files.",
    )
    args = parser.parse_args(argv)

    vault_path = _resolve_vault(args.vault)
    if not vault_path.is_dir():
        print(f"error: vault not found: {vault_path}", file=sys.stderr)
        return 2

    out_dir = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir
        else vault_path / "_meta" / "audits"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    vault = Vault(vault_path)
    db_path = vault_path / "_meta" / "index.db"
    if not db_path.is_file():
        print(f"error: index not found at {db_path}", file=sys.stderr)
        return 2
    index = Index(db_path)
    index.connect()

    try:
        print(f"audit: reading vault at {vault_path}")
        views = collect_views(vault, index)
        print(f"audit: {len(views)} record(s) read")
        groups = classify(views)
        print(f"audit: {len(groups)} candidate group(s) identified")

        generated = datetime.now(tz=UTC)
        stamp = generated.strftime("%Y%m%dT%H%M%SZ")
        md_path = out_dir / f"dedupe-audit-{stamp}.md"
        json_path = out_dir / f"dedupe-audit-{stamp}.json"

        md_path.write_text(
            render_markdown(
                vault_path=vault_path,
                views=views,
                groups=groups,
                generated=generated,
                top_n_per_class=args.top_n,
            ),
            encoding="utf-8",
        )
        json_path.write_text(
            render_json(
                vault_path=vault_path,
                views=views,
                groups=groups,
                generated=generated,
            ),
            encoding="utf-8",
        )

        print(f"audit: markdown report → {md_path}")
        print(f"audit: json report     → {json_path}")

        if args.print_summary or os.isatty(sys.stdout.fileno()):
            print()
            print("=" * 60)
            print("Class summary")
            print("=" * 60)
            for klass in CLASS_BY_KEY:
                summary = _summary_row(klass, groups)
                print(
                    f"  {klass}  groups={summary['groups']:>4d}  "
                    f"records={summary['records']:>5d}  "
                    f"action={summary['recommended_action']}  "
                    f"({summary['title']})"
                )
    finally:
        index.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
