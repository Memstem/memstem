"""Tests for ``scripts/dedupe_phase1_select.py``.

The Phase-1 selector is the gate between the broad audit (which can
contain false positives) and any future quarantine action. These tests
pin the strict-filter contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_phase1_select.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("dedupe_phase1_select", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dedupe_phase1_select"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> object:
    return _load_module()


def _member(
    *,
    member_id: str,
    type_: str = "memory",
    title: str | None = "T",
    path: str | None = None,
    body_hash: str = "h",
    provenance_source: str | None = "openclaw",
    provenance_ref: str | None = "/x.md",
    deprecated_by: str | None = None,
    importance: float | None = 0.5,
    retrievals: int = 0,
    ingested_at: str | None = "2026-04-28T10:00:00+00:00",
    updated: str | None = "2026-04-28T10:00:00+00:00",
) -> dict[str, Any]:
    return {
        "id": member_id,
        "type": type_,
        "source": provenance_source,
        "title": title,
        "path": path or f"memories/{member_id}.md",
        "body_hash": body_hash,
        "provenance_source": provenance_source,
        "provenance_ref": provenance_ref,
        "provenance_ingested_at": ingested_at,
        "created": ingested_at,
        "updated": updated,
        "importance": importance,
        "retrievals": retrievals,
        "deprecated_by": deprecated_by,
        "has_links": False,
        "tag_count": 0,
    }


def _group(
    *,
    klass: str = "B",
    members: list[dict[str, Any]],
    winner_id: str | None = None,
    coin_flip: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "class": klass,
        "confidence": "high",
        "risk": "low",
        "rationale": "test",
        "recommended_action": "safe_quarantine_candidate",
        "winner_id": winner_id or members[0]["id"],
        "coin_flip": coin_flip,
        "extra": extra or {},
        "members": members,
    }


def _audit(groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-05-01T00:00:00+00:00",
        "vault": "/tmp/test-vault",
        "totals": {"records": 99, "by_type": {"memory": 99}, "deprecated": 0},
        "classes": [],
        "groups": groups,
    }


# ─── Acceptance ──────────────────────────────────────────────────────


def test_canonical_phase1_group_accepted(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a"),
                    _member(member_id="b"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 1
    assert manifest["totals"]["total_loser_records"] == 1
    g = manifest["groups"][0]
    assert g["winner"]["id"] == "a"
    assert [loser["id"] for loser in g["losers"]] == ["b"]
    assert g["phase1_qualifies"] is True


# ─── Rejection paths (one per rule) ──────────────────────────────────


def test_rejects_other_classes(mod: object) -> None:
    audit = _audit(
        [
            _group(
                klass="A",
                members=[_member(member_id="a"), _member(member_id="b")],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    # Class A groups never enter Phase 1 unless they're also Class B
    # in the audit (which the audit produces separately).
    assert manifest["totals"]["accepted_groups"] == 0
    assert manifest["totals"]["candidate_groups_in_source_class"] == 0


def test_rejects_blocked_type_skill(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", type_="skill"),
                    _member(member_id="b", type_="skill"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0
    reasons = manifest["rejected"][0]["reasons"]
    assert any("blocked type: skill" in r for r in reasons)


@pytest.mark.parametrize("blocked", ["project", "distillation"])
def test_rejects_blocked_types_project_distillation(mod: object, blocked: str) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", type_=blocked),
                    _member(member_id="b", type_=blocked),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0


def test_rejects_mixed_provenance_ref(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", provenance_ref="/path/one.md"),
                    _member(member_id="b", provenance_ref="/path/two.md"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0
    reasons = manifest["rejected"][0]["reasons"]
    assert any("provenance.ref" in r for r in reasons)


def test_rejects_missing_provenance_ref(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", provenance_ref=None),
                    _member(member_id="b", provenance_ref=None),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0


def test_rejects_mixed_provenance_source(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", provenance_source="openclaw"),
                    _member(member_id="b", provenance_source="claude-code"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0


def test_rejects_coin_flip_winner(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a"),
                    _member(member_id="b"),
                ],
                winner_id="a",
                coin_flip=True,
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0
    reasons = manifest["rejected"][0]["reasons"]
    assert any("coin-flip" in r for r in reasons)


def test_rejects_already_deprecated_member(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a"),
                    _member(member_id="b", deprecated_by="zzz"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0


def test_rejects_mixed_types(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", type_="memory"),
                    _member(member_id="b", type_="session"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    assert manifest["totals"]["accepted_groups"] == 0


# ─── Manifest shape + dry-run plan ───────────────────────────────────


def test_manifest_includes_provenance_for_each_winner(mod: object) -> None:
    audit = _audit(
        [
            _group(
                members=[
                    _member(member_id="a", title="My title"),
                    _member(member_id="b", title="My title"),
                ],
                winner_id="a",
            )
        ]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    g = manifest["groups"][0]
    assert g["provenance_source"] == "openclaw"
    assert g["provenance_ref"] == "/x.md"
    assert g["winner"]["title"] == "My title"
    assert g["losers"][0]["title"] == "My title"


def test_dry_run_plan_mentions_no_mutation(mod: object) -> None:
    audit = _audit(
        [_group(members=[_member(member_id="a"), _member(member_id="b")], winner_id="a")]
    )
    manifest = mod.select_phase1(audit)  # type: ignore[attr-defined]
    plan_text = mod.render_plan(manifest)  # type: ignore[attr-defined]
    assert "No files have been modified" in plan_text
    assert "DRY-RUN" in plan_text
    assert "deprecated_by" in plan_text


def test_no_apply_path_in_module(mod: object) -> None:
    """Belt-and-suspenders: the selector must not expose an --apply path."""
    forbidden = {"apply", "apply_phase1", "quarantine", "delete_record"}
    assert not (forbidden & set(dir(mod)))


def test_main_writes_artifacts_and_does_not_create_an_apply_flag(
    mod: object, tmp_path: Path
) -> None:
    audit = _audit(
        [_group(members=[_member(member_id="a"), _member(member_id="b")], winner_id="a")]
    )
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = mod.main(  # type: ignore[attr-defined]
        ["--audit-json", str(audit_path), "--out-dir", str(out_dir)]
    )
    assert rc == 0
    manifests = list(out_dir.glob("phase1-manifest-*.json"))
    reports = list(out_dir.glob("phase1-report-*.md"))
    assert len(manifests) == 1
    assert len(reports) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["totals"]["accepted_groups"] == 1


def test_argparse_does_not_have_apply_flag(mod: object, tmp_path: Path) -> None:
    """The selector must explicitly reject any --apply argument."""
    audit_path = tmp_path / "a.json"
    audit_path.write_text(
        json.dumps(_audit([])),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        mod.main(["--audit-json", str(audit_path), "--apply"])  # type: ignore[attr-defined]
