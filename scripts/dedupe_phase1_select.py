#!/usr/bin/env python3
"""Phase-1 candidate selector for the dedupe audit.

Reads the JSON output of ``scripts/dedupe_audit_report.py`` and emits
a strictly-filtered manifest of Phase-1-safe duplicate groups — only
the groups conservative enough for near-mechanical quarantine review.

Phase-1 inclusion rules (all must hold for a group to make the cut):

1. Same record ``type`` for every member.
2. Same ``provenance.source`` for every member.
3. Same ``provenance.ref`` for every member.
4. Same normalized body hash for every member (always true for groups
   produced by the audit's body-hash bucketing; verified again here).
5. No member already carries ``deprecated_by``.
6. ``type`` is not in {skill, project, distillation}.
7. The group is single-class (no cross-type collisions).
8. Winner selection is unambiguous (``coin_flip == False``).
9. No mixed source-root paths — covered by (2) + (3).

This script writes nothing back to the vault. It produces a manifest
JSON + markdown report for human review. There is no ``--apply`` flag
on this script by design: quarantine-time mutation must go through a
separately-audited path (today: ``memstem hygiene cleanup-retro --apply``,
operated by the user after they've reviewed the manifest).

Usage:

    python3 scripts/dedupe_phase1_select.py [--audit-json PATH]
                                             [--out-dir PATH]
                                             [--show-plan]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_VAULT = Path.home() / "memstem-vault"

# Types that must never enter Phase 1, even if all other checks pass.
# Skills are high-leverage and require an operator review queue per
# ADR 0012; projects and distillations are curated rollups whose
# byte equality is intentional.
PHASE1_BLOCKED_TYPES = frozenset({"skill", "project", "distillation"})

# Classes from the source audit that signal a body-hash collision the
# Phase-1 selector wants to *consider*. We start from Class B (same
# provenance.ref + same body hash) because that is the only class that
# already enforces ref-equality. Class A groups that aren't also Class B
# represent the textbook false-positive pattern (same body, different
# source location) and are excluded.
SOURCE_CLASS = "B"


def _resolve_vault(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("MEMSTEM_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_VAULT


def _latest_audit_json(vault: Path) -> Path | None:
    audits = vault / "_meta" / "audits"
    if not audits.is_dir():
        return None
    candidates = sorted(audits.glob("dedupe-audit-*.json"))
    return candidates[-1] if candidates else None


def _disqualify_reasons(group: dict[str, Any]) -> list[str]:
    """Return the list of Phase-1 rules a group fails.

    Empty list means the group is Phase-1-safe.
    """
    reasons: list[str] = []
    members = group["members"]
    if len(members) < 2:
        reasons.append("singleton (no duplicate)")

    types = {m["type"] for m in members}
    sources = {m["provenance_source"] for m in members}
    refs = {m["provenance_ref"] for m in members}
    hashes = {m["body_hash"] for m in members}

    if len(types) != 1:
        reasons.append(f"mixed types: {sorted(types)}")
    elif next(iter(types)) in PHASE1_BLOCKED_TYPES:
        reasons.append(f"blocked type: {next(iter(types))}")

    if len(sources) != 1 or None in sources:
        reasons.append(f"mixed/missing provenance.source: {sorted(s or '∅' for s in sources)}")
    if len(refs) != 1 or None in refs:
        reasons.append("mixed/missing provenance.ref")
    if len(hashes) != 1:
        reasons.append("body hashes differ")

    if any(m.get("deprecated_by") for m in members):
        reasons.append("contains already-deprecated record")

    if group.get("coin_flip"):
        reasons.append("coin-flip winner (no clear canonical)")

    if not group.get("winner_id"):
        reasons.append("no winner_id assigned")

    return reasons


def select_phase1(audit: dict[str, Any]) -> dict[str, Any]:
    """Return the Phase-1 manifest derived from an audit dict.

    The manifest contains one entry per duplicate group, with the
    fields needed for human review and for any future filtered apply
    pass.
    """
    candidates = [g for g in audit["groups"] if g["class"] == SOURCE_CLASS]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for g in candidates:
        reasons = _disqualify_reasons(g)
        if reasons:
            rejected.append({"group": g, "reasons": reasons})
            continue
        members = g["members"]
        winner_id = g["winner_id"]
        winner = next(m for m in members if m["id"] == winner_id)
        losers = [m for m in members if m["id"] != winner_id]
        only_type = members[0]["type"]
        prov_source = members[0]["provenance_source"]
        prov_ref = members[0]["provenance_ref"]
        body_hash = members[0]["body_hash"]
        accepted.append(
            {
                "type": only_type,
                "provenance_source": prov_source,
                "provenance_ref": prov_ref,
                "body_hash": body_hash,
                "winner": {
                    "id": winner["id"],
                    "path": winner["path"],
                    "title": winner["title"],
                    "ingested_at": winner["provenance_ingested_at"],
                    "updated": winner["updated"],
                    "importance": winner["importance"],
                    "retrievals": winner["retrievals"],
                },
                "losers": [
                    {
                        "id": m["id"],
                        "path": m["path"],
                        "title": m["title"],
                        "ingested_at": m["provenance_ingested_at"],
                        "updated": m["updated"],
                        "importance": m["importance"],
                        "retrievals": m["retrievals"],
                    }
                    for m in losers
                ],
                "rationale": (
                    "same type, same provenance.source, same provenance.ref, "
                    "same normalized body hash, no deprecation, no coin-flip, "
                    "type not in blocked set"
                ),
                "phase1_qualifies": True,
            }
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "source_audit": {
            "generated_at": audit.get("generated_at"),
            "vault": audit.get("vault"),
            "totals": audit.get("totals"),
        },
        "selection": {
            "rule_set_version": "phase1.v1",
            "blocked_types": sorted(PHASE1_BLOCKED_TYPES),
            "source_class": SOURCE_CLASS,
            "extra_constraints": [
                "single type per group",
                "single provenance.source per group",
                "single provenance.ref per group",
                "single body hash per group",
                "no member with deprecated_by",
                "no coin-flip winner",
            ],
        },
        "totals": {
            "candidate_groups_in_source_class": len(candidates),
            "accepted_groups": len(accepted),
            "rejected_groups": len(rejected),
            "total_loser_records": sum(len(g["losers"]) for g in accepted),
        },
        "groups": accepted,
        "rejected": [
            {
                "class": r["group"]["class"],
                "winner_id": r["group"]["winner_id"],
                "size": len(r["group"]["members"]),
                "reasons": r["reasons"],
            }
            for r in rejected
        ],
    }


def render_markdown(manifest: dict[str, Any], top_n: int = 12) -> str:
    totals = manifest["totals"]
    src = manifest["source_audit"]
    lines: list[str] = []
    lines.append("# Phase-1 dedupe candidates (read-only)")
    lines.append("")
    lines.append(f"- Generated: `{manifest['generated_at']}`")
    lines.append(f"- Source audit: `{src.get('generated_at')}`  ·  vault: `{src.get('vault')}`")
    lines.append(f"- Rule set: **{manifest['selection']['rule_set_version']}**")
    lines.append("")
    lines.append(
        f"**{totals['accepted_groups']}** Phase-1-safe groups · "
        f"**{totals['total_loser_records']}** candidate loser records · "
        f"{totals['rejected_groups']} group(s) rejected from the source class."
    )
    lines.append("")
    lines.append("> No file or index has been mutated. This manifest is the gate.")
    lines.append("")

    # Inclusion rules — restated next to the data they describe.
    lines.append("## Inclusion rules (all must hold)")
    lines.append("")
    lines.append(f"- Source class in audit: **{manifest['selection']['source_class']}**")
    for rule in manifest["selection"]["extra_constraints"]:
        lines.append(f"- {rule}")
    lines.append("- type ∉ {" + ", ".join(sorted(manifest["selection"]["blocked_types"])) + "}")
    lines.append("")

    # Coverage breakdown.
    type_counter: Counter[str] = Counter(g["type"] for g in manifest["groups"])
    lines.append("## Coverage")
    lines.append("")
    lines.append("| type | groups | losers |")
    lines.append("|------|--------|--------|")
    for t, n in type_counter.most_common():
        losers = sum(len(g["losers"]) for g in manifest["groups"] if g["type"] == t)
        lines.append(f"| {t} | {n} | {losers} |")
    lines.append("")

    # Top groups (for quick eyeballing).
    if manifest["groups"]:
        sorted_groups = sorted(
            manifest["groups"],
            key=lambda g: (-len(g["losers"]), g["winner"]["id"]),
        )
        shown = sorted_groups[:top_n]
        if len(sorted_groups) > len(shown):
            lines.append(f"## First {len(shown)} of {len(sorted_groups)} candidate groups")
        else:
            lines.append(f"## All {len(sorted_groups)} candidate groups")
        lines.append("")
        lines.append("Each row is one duplicate set. Winner is the proposed canonical.")
        lines.append("")
        for i, g in enumerate(shown, start=1):
            title = (g["winner"]["title"] or "(no title)").replace("|", "\\|")[:80]
            ref = (g["provenance_ref"] or "—").replace("|", "\\|")[:80]
            lines.append(f"### Group {i} — {title}")
            lines.append("")
            lines.append(f"- type: `{g['type']}`")
            lines.append(f"- provenance.source: `{g['provenance_source']}`")
            lines.append(f"- provenance.ref: `{ref}`")
            lines.append(f"- body_hash: `{g['body_hash'][:16]}…`")
            lines.append("")
            lines.append("| role | id | path | ingested_at | updated | importance |")
            lines.append("|------|----|------|-------------|---------|------------|")
            w = g["winner"]
            lines.append(
                f"| winner | `{w['id']}` | `{w['path']}` | "
                f"{(w['ingested_at'] or '—')[:19]} | "
                f"{(w['updated'] or '—')[:19]} | "
                f"{w['importance'] if w['importance'] is not None else '—'} |"
            )
            for m in g["losers"]:
                lines.append(
                    f"| loser | `{m['id']}` | `{m['path']}` | "
                    f"{(m['ingested_at'] or '—')[:19]} | "
                    f"{(m['updated'] or '—')[:19]} | "
                    f"{m['importance'] if m['importance'] is not None else '—'} |"
                )
            lines.append("")

    # Rejected groups summary.
    if manifest["rejected"]:
        reason_counter: Counter[str] = Counter()
        for r in manifest["rejected"]:
            for reason in r["reasons"]:
                reason_counter[reason] += 1
        lines.append("## Rejected from source class (excluded from Phase 1)")
        lines.append("")
        lines.append(f"{len(manifest['rejected'])} group(s) rejected. Reason breakdown:")
        lines.append("")
        for reason, n in reason_counter.most_common():
            lines.append(f"- {reason}: **{n}**")
        lines.append("")

    return "\n".join(lines)


def render_plan(manifest: dict[str, Any]) -> str:
    """Render a dry-run plan describing what a future quarantine pass would do.

    No mutations occur. This is purely descriptive output for human review.
    """
    lines: list[str] = []
    n = len(manifest["groups"])
    losers = sum(len(g["losers"]) for g in manifest["groups"])
    lines.append(f"DRY-RUN: Phase-1 quarantine plan ({n} groups → {losers} deprecations)")
    lines.append("")
    lines.append(
        "Each loser would gain `deprecated_by: <winner_id>` in its frontmatter. "
        "Both files stay on disk; default search filters losers via the "
        "existing `deprecated_by` filter."
    )
    lines.append("")
    lines.append("This is dry-run output. No files have been modified.")
    lines.append("")
    for i, g in enumerate(manifest["groups"], start=1):
        lines.append(f"[{i}/{n}] type={g['type']}  ref={g['provenance_ref']}")
        lines.append(f"    winner: {g['winner']['id']}  →  {g['winner']['path']}")
        for m in g["losers"]:
            lines.append(
                f"    loser:  {m['id']}  →  {m['path']}\n"
                f"            would set: deprecated_by = {g['winner']['id']}"
            )
    lines.append("")
    lines.append("To execute when approved (after a snapshot — see docs/dedupe-audit.md):")
    lines.append("")
    lines.append("    cp -a $MEMSTEM_VAULT $MEMSTEM_VAULT.audit-snap-$(date +%Y%m%dT%H%M%SZ)")
    lines.append("    memstem hygiene cleanup-retro --no-noise          # dry-run")
    lines.append("    memstem hygiene cleanup-retro --no-noise --apply  # writes deprecated_by")
    lines.append("")
    lines.append(
        "NOTE: `cleanup-retro` today operates on the whole vault; a future "
        "`--from-manifest` option could constrain it to this Phase-1 set. "
        "Until that lands, run `cleanup-retro` without --apply, diff its "
        "report against this manifest, and only proceed if the planned set "
        "is a strict superset of these groups."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        help="Vault path used to locate the audit JSON (default: $MEMSTEM_VAULT or ~/memstem-vault).",
    )
    parser.add_argument(
        "--audit-json",
        help="Path to a specific audit JSON. Defaults to the most recent file in <vault>/_meta/audits/.",
    )
    parser.add_argument(
        "--out-dir",
        help="Output directory for manifest + report (default: <vault>/_meta/audits/).",
    )
    parser.add_argument(
        "--show-plan",
        action="store_true",
        help="Print the dry-run quarantine plan to stdout. Mutates nothing.",
    )
    args = parser.parse_args(argv)

    vault = _resolve_vault(args.vault)
    if args.audit_json:
        audit_path = Path(args.audit_json).expanduser().resolve()
    else:
        latest = _latest_audit_json(vault)
        if latest is None:
            print(
                "error: no audit JSON found. Run scripts/dedupe_audit_report.py first.",
                file=sys.stderr,
            )
            return 2
        audit_path = latest

    if not audit_path.is_file():
        print(f"error: audit JSON not found: {audit_path}", file=sys.stderr)
        return 2

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    manifest = select_phase1(audit)

    out_dir = (
        Path(args.out_dir).expanduser().resolve() if args.out_dir else vault / "_meta" / "audits"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = out_dir / f"phase1-manifest-{stamp}.json"
    md_path = out_dir / f"phase1-report-{stamp}.md"

    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(manifest), encoding="utf-8")

    print(f"phase1: source audit  → {audit_path}")
    print(f"phase1: manifest      → {manifest_path}")
    print(f"phase1: markdown      → {md_path}")
    print(
        f"phase1: accepted={manifest['totals']['accepted_groups']}  "
        f"losers={manifest['totals']['total_loser_records']}  "
        f"rejected={manifest['totals']['rejected_groups']}"
    )

    if args.show_plan:
        print()
        print(render_plan(manifest))

    return 0


if __name__ == "__main__":
    sys.exit(main())
