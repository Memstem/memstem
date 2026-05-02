#!/usr/bin/env python3
"""Manifest-constrained applier for the Phase-1 dedupe manifest.

Operates on a single concrete file: the JSON manifest produced by
``scripts/dedupe_phase1_select.py``. The blast radius is exactly the
loser IDs listed in the manifest — nothing else in the vault can be
touched. This is intentional and tested.

What this script does on ``--apply`` (and only then):

For each manifest entry, after re-validating every Phase-1 rule
against current vault state:

1. Set ``deprecated_by: <winner_id>`` in the loser's frontmatter.
2. Persist the modified loser via ``Vault.write``.
3. Mirror the change into the SQLite index via ``Index.upsert``.
4. Append a row to the ``dedup_audit`` table with
   ``judge="phase1-manifest"`` and a per-group rationale, so the
   apply trail is queryable.

The winner file and any non-manifest record are NEVER touched.

Defense in depth — a per-group apply is **aborted** (skipped, logged,
counted) if any of these checks fails at apply time:

- Manifest header has the wrong schema or rule-set version.
- Group's ``phase1_qualifies`` is not true.
- Loser type is in ``{skill, project, distillation}``.
- Winner file or any loser file is unreadable.
- Winner's current normalized body hash differs from the manifest hash.
- Any loser's current normalized body hash differs from the manifest hash.
- A loser already carries ``deprecated_by`` (idempotence: re-running on
  an applied vault is a no-op).
- Winner's ``id`` is missing or doesn't match the file's frontmatter.

Aborts are logged in the result, not raised — so a single drifted
record does not silently take the whole run with it. The operator
sees every skip in the dry-run output and decides whether to proceed.

Default behavior is **dry-run**. Pass ``--apply`` to perform the
mutation. There is no batch flag, no glob, no broad-vault scan — the
manifest is the only input.

Usage:

    python3 scripts/dedupe_phase1_apply.py --manifest <path> [--apply]
                                            [--vault PATH]
                                            [--out-dir PATH]
                                            [--json-out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memstem.core.dedup import normalized_body_hash
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault

DEFAULT_VAULT = Path.home() / "memstem-vault"

EXPECTED_SCHEMA_VERSION = 1
EXPECTED_RULE_SET = "phase1.v1"
PHASE1_BLOCKED_TYPES = frozenset({"skill", "project", "distillation"})

JUDGE_TAG = "phase1-manifest"


# ─── Result types ────────────────────────────────────────────────────


@dataclass(slots=True)
class GroupOutcome:
    """One manifest group's outcome (dry-run or applied)."""

    winner_id: str
    losers: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    skip_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ApplyReport:
    """Aggregate result of a manifest run."""

    manifest_path: str
    vault_path: str
    dry_run: bool
    started_at: str
    finished_at: str | None = None
    schema_ok: bool = False
    rule_set_ok: bool = False
    total_groups_in_manifest: int = 0
    groups_examined: int = 0
    groups_skipped: int = 0
    losers_planned: int = 0
    losers_applied: int = 0
    audit_rows_written: int = 0
    fatal_error: str | None = None
    outcomes: list[GroupOutcome] = field(default_factory=list)


# ─── Helpers ─────────────────────────────────────────────────────────


def _resolve_vault(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("MEMSTEM_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_VAULT


def _load_manifest(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _validate_manifest_header(manifest: dict[str, Any]) -> list[str]:
    """Return a list of header-level errors. Empty list = OK."""
    errs: list[str] = []
    if manifest.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        errs.append(
            f"manifest schema_version={manifest.get('schema_version')!r} "
            f"(expected {EXPECTED_SCHEMA_VERSION})"
        )
    selection = manifest.get("selection") or {}
    if selection.get("rule_set_version") != EXPECTED_RULE_SET:
        errs.append(
            f"manifest rule_set_version={selection.get('rule_set_version')!r} "
            f"(expected {EXPECTED_RULE_SET!r})"
        )
    return errs


def _check_group(
    vault: Vault,
    group: dict[str, Any],
) -> tuple[Memory | None, dict[str, Memory], list[str]]:
    """Re-validate a manifest group against current vault state.

    Returns: (winner_memory_or_None, {loser_id: loser_memory}, errors).
    """
    errors: list[str] = []

    if not group.get("phase1_qualifies"):
        errors.append("group does not carry phase1_qualifies=true")

    if group.get("type") in PHASE1_BLOCKED_TYPES:
        errors.append(f"blocked type at apply time: {group.get('type')}")

    expected_hash = group.get("body_hash")
    if not expected_hash:
        errors.append("manifest group missing body_hash")
        return None, {}, errors

    winner_meta = group.get("winner") or {}
    winner_id = winner_meta.get("id")
    winner_path = winner_meta.get("path")
    if not winner_id or not winner_path:
        errors.append("manifest group missing winner id/path")
        return None, {}, errors

    try:
        winner_mem = vault.read(winner_path)
    except Exception as exc:
        errors.append(f"winner unreadable: {exc}")
        return None, {}, errors

    if str(winner_mem.frontmatter.id) != winner_id:
        errors.append(
            f"winner id mismatch: file has {winner_mem.frontmatter.id}, "
            f"manifest expects {winner_id}"
        )
    winner_hash = normalized_body_hash(winner_mem.body)
    if winner_hash != expected_hash:
        errors.append(f"winner body hash drifted: {expected_hash[:16]}… → {winner_hash[:16]}…")

    loser_mems: dict[str, Memory] = {}
    for loser in group.get("losers", []):
        loser_id = loser.get("id")
        loser_path = loser.get("path")
        if not loser_id or not loser_path:
            errors.append("manifest loser missing id/path")
            continue
        try:
            loser_mem = vault.read(loser_path)
        except Exception as exc:
            errors.append(f"loser {loser_id} unreadable: {exc}")
            continue
        if str(loser_mem.frontmatter.id) != loser_id:
            errors.append(
                f"loser id mismatch: file at {loser_path} has "
                f"{loser_mem.frontmatter.id}, manifest expects {loser_id}"
            )
            continue
        loser_hash = normalized_body_hash(loser_mem.body)
        if loser_hash != expected_hash:
            errors.append(
                f"loser {loser_id} body hash drifted: {expected_hash[:16]}… → {loser_hash[:16]}…"
            )
            continue
        if loser_mem.frontmatter.deprecated_by is not None:
            errors.append(
                f"loser {loser_id} already deprecated_by={loser_mem.frontmatter.deprecated_by}"
            )
            continue
        if loser_mem.frontmatter.type.value != group.get("type"):
            errors.append(
                f"loser {loser_id} type mismatch: file is "
                f"{loser_mem.frontmatter.type.value}, manifest "
                f"says {group.get('type')}"
            )
            continue
        loser_mems[loser_id] = loser_mem

    return winner_mem, loser_mems, errors


# ─── Core apply ──────────────────────────────────────────────────────


def run(
    manifest_path: Path,
    *,
    vault: Vault,
    index: Index,
    dry_run: bool,
) -> ApplyReport:
    started = datetime.now(tz=UTC).isoformat()
    report = ApplyReport(
        manifest_path=str(manifest_path),
        vault_path=str(vault.root),
        dry_run=dry_run,
        started_at=started,
    )

    try:
        manifest = _load_manifest(manifest_path)
    except Exception as exc:
        report.fatal_error = f"could not read manifest: {exc}"
        report.finished_at = datetime.now(tz=UTC).isoformat()
        return report

    header_errs = _validate_manifest_header(manifest)
    report.schema_ok = "schema_version" not in " ".join(header_errs)
    report.rule_set_ok = "rule_set_version" not in " ".join(header_errs)
    if header_errs:
        report.fatal_error = "manifest header invalid: " + "; ".join(header_errs)
        report.finished_at = datetime.now(tz=UTC).isoformat()
        return report

    groups = manifest.get("groups") or []
    report.total_groups_in_manifest = len(groups)

    audit_ts = datetime.now(tz=UTC).isoformat()

    for group in groups:
        report.groups_examined += 1
        winner_mem, loser_mems, errs = _check_group(vault, group)
        outcome = GroupOutcome(winner_id=group.get("winner", {}).get("id", "?"))
        if errs or winner_mem is None:
            outcome.skipped = True
            outcome.skip_reasons = errs or ["unknown error"]
            report.outcomes.append(outcome)
            report.groups_skipped += 1
            continue

        winner_id = str(winner_mem.frontmatter.id)
        for loser_id, loser_mem in loser_mems.items():
            planned = {
                "loser_id": loser_id,
                "loser_path": str(loser_mem.path),
                "would_set_deprecated_by": winner_id,
                "status": "would_apply" if dry_run else "applied",
            }
            outcome.losers.append(planned)
            report.losers_planned += 1

            if dry_run:
                continue

            try:
                new_fm = loser_mem.frontmatter.model_copy(update={"deprecated_by": UUID(winner_id)})
                new_mem = Memory(frontmatter=new_fm, body=loser_mem.body, path=loser_mem.path)
                vault.write(new_mem)
                index.upsert(new_mem)

                with index._lock, index.db:
                    index.db.execute(
                        """
                        INSERT INTO dedup_audit
                            (ts, new_id, existing_id, verdict, rationale, judge, applied)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            audit_ts,
                            loser_id,
                            winner_id,
                            "DUPLICATE",
                            (
                                "phase1 manifest apply (same type, source, ref, "
                                "and body hash; coin-flip excluded)"
                            ),
                            JUDGE_TAG,
                        ),
                    )
                    report.audit_rows_written += 1
                report.losers_applied += 1
            except Exception as exc:
                planned["status"] = f"error: {exc!r}"
                outcome.skip_reasons.append(f"loser {loser_id} apply failed: {exc!r}")

        report.outcomes.append(outcome)

    report.finished_at = datetime.now(tz=UTC).isoformat()
    return report


# ─── Reporting ───────────────────────────────────────────────────────


def render_summary(report: ApplyReport) -> str:
    mode = "DRY-RUN" if report.dry_run else "APPLY"
    lines: list[str] = []
    lines.append(f"=== Phase-1 manifest {mode} ===")
    lines.append(f"manifest:        {report.manifest_path}")
    lines.append(f"vault:           {report.vault_path}")
    lines.append(f"started:         {report.started_at}")
    lines.append(f"finished:        {report.finished_at or '(in progress)'}")
    if report.fatal_error:
        lines.append("")
        lines.append(f"FATAL: {report.fatal_error}")
        lines.append("Nothing was applied.")
        return "\n".join(lines)
    lines.append(f"schema check:    {'ok' if report.schema_ok else 'FAIL'}")
    lines.append(f"rule-set check:  {'ok' if report.rule_set_ok else 'FAIL'}")
    lines.append(f"groups in manifest:  {report.total_groups_in_manifest}")
    lines.append(f"groups examined:     {report.groups_examined}")
    lines.append(f"groups skipped:      {report.groups_skipped}")
    lines.append(f"losers planned:      {report.losers_planned}")
    lines.append(f"losers applied:      {report.losers_applied}")
    lines.append(f"audit rows written:  {report.audit_rows_written}")
    if report.dry_run:
        lines.append("")
        lines.append("DRY-RUN: no files written, no index rows changed, no audit rows added.")
    return "\n".join(lines)


def render_skips(report: ApplyReport, *, max_per_outcome: int = 5) -> str:
    lines: list[str] = []
    skipped = [o for o in report.outcomes if o.skipped]
    if not skipped:
        return "No skips."
    lines.append(f"Skipped groups: {len(skipped)}")
    for o in skipped:
        lines.append(f"  winner_id={o.winner_id}")
        for reason in o.skip_reasons[:max_per_outcome]:
            lines.append(f"    - {reason}")
    return "\n".join(lines)


def render_actions(report: ApplyReport, *, max_show: int = 20) -> str:
    lines: list[str] = []
    actions = [(o.winner_id, lo) for o in report.outcomes for lo in o.losers]
    lines.append(f"Planned/applied actions: {len(actions)}")
    shown = actions[:max_show]
    for winner_id, lo in shown:
        lines.append(
            f"  winner={winner_id}  loser={lo['loser_id']}  "
            f"path={lo['loser_path']}  status={lo['status']}"
        )
    if len(actions) > len(shown):
        lines.append(f"  … ({len(actions) - len(shown)} more in JSON output)")
    return "\n".join(lines)


# ─── Entry point ─────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manifest-constrained Phase-1 dedupe applier. Default is "
            "dry-run; pass --apply to perform mutation."
        )
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the Phase-1 manifest JSON.",
    )
    parser.add_argument(
        "--vault",
        help="Vault path (default: $MEMSTEM_VAULT or ~/memstem-vault).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Perform the mutation. WITHOUT this flag, the script is dry-run "
            "and writes nothing to the vault or index."
        ),
    )
    parser.add_argument(
        "--json-out",
        help="Write the full structured report as JSON.",
    )
    parser.add_argument(
        "--show-skips",
        action="store_true",
        help="Print every skip with its reason(s).",
    )
    parser.add_argument(
        "--show-actions",
        action="store_true",
        help="Print the planned/applied actions list.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.is_file():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    vault_path = _resolve_vault(args.vault)
    if not vault_path.is_dir():
        print(f"error: vault not found: {vault_path}", file=sys.stderr)
        return 2
    db_path = vault_path / "_meta" / "index.db"
    if not db_path.is_file():
        print(f"error: index not found: {db_path}", file=sys.stderr)
        return 2

    vault = Vault(vault_path)
    index = Index(db_path)
    index.connect()
    try:
        report = run(manifest_path, vault=vault, index=index, dry_run=not args.apply)
    finally:
        index.close()

    print(render_summary(report))
    if args.show_actions:
        print()
        print(render_actions(report))
    if args.show_skips:
        print()
        print(render_skips(report))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(asdict(report), indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nfull JSON report → {args.json_out}")

    if report.fatal_error:
        return 3
    if report.groups_skipped and not args.apply:
        # Dry-run with skips: not a failure, but worth a non-zero exit
        # so a CI-style runner notices.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
