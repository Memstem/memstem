"""Tests for ``scripts/dedupe_phase1_apply.py``.

Pinned invariants:

1. Default mode is dry-run; the vault and index are unchanged.
2. ``--apply`` only mutates files whose IDs appear in the manifest.
3. A drifted loser hash aborts that group only (not the whole run).
4. An already-deprecated loser is skipped (idempotent re-runs).
5. A blocked-type entry in the manifest is rejected at apply time.
6. The audit log gets one row per applied loser, tagged
   ``judge="phase1-manifest"``.
7. Manifest schema/rule-set mismatches abort fatally before any work.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from memstem.core.dedup import normalized_body_hash
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_phase1_apply.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("dedupe_phase1_apply", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dedupe_phase1_apply"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> object:
    return _load_module()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(vault: Vault) -> Iterator[Index]:
    idx = Index(vault.root / "_meta" / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


def _write_pair(
    vault: Vault,
    index: Index,
    *,
    body: str,
    type_: str = "memory",
    provenance_ref: str = "/x.md",
    importance_winner: float = 0.9,
    importance_loser: float = 0.1,
) -> tuple[Memory, Memory]:
    """Write two records with identical bodies — winner gets higher importance."""

    def make(importance: float) -> Memory:
        record_id = str(uuid4())
        metadata: dict[str, Any] = {
            "id": record_id,
            "type": type_,
            "created": "2026-04-28T12:00:00+00:00",
            "updated": "2026-04-28T12:00:00+00:00",
            "source": "openclaw",
            "title": "T",
            "importance": importance,
            "provenance": {"source": "openclaw", "ref": provenance_ref},
        }
        if type_ == "skill":
            metadata["scope"] = "universal"
            metadata["verification"] = "verify by hand"
        fm = validate(metadata)
        path = Path(f"memories/{record_id}.md")
        memory = Memory(frontmatter=fm, body=body, path=path)
        vault.write(memory)
        index.upsert(memory)
        return memory

    return make(importance_winner), make(importance_loser)


def _manifest(
    *,
    winner: Memory,
    loser: Memory,
    rule_set_version: str = "phase1.v1",
    schema_version: int = 1,
    phase1_qualifies: bool = True,
    type_: str | None = None,
) -> dict[str, Any]:
    body_hash = normalized_body_hash(winner.body)
    return {
        "schema_version": schema_version,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "source_audit": {},
        "selection": {
            "rule_set_version": rule_set_version,
            "blocked_types": ["distillation", "project", "skill"],
            "source_class": "B",
            "extra_constraints": [],
        },
        "totals": {
            "candidate_groups_in_source_class": 1,
            "accepted_groups": 1,
            "rejected_groups": 0,
            "total_loser_records": 1,
        },
        "groups": [
            {
                "type": type_ or winner.frontmatter.type.value,
                "provenance_source": "openclaw",
                "provenance_ref": "/x.md",
                "body_hash": body_hash,
                "winner": {
                    "id": str(winner.frontmatter.id),
                    "path": str(winner.path),
                    "title": winner.frontmatter.title,
                    "ingested_at": None,
                    "updated": "2026-04-28T12:00:00+00:00",
                    "importance": winner.frontmatter.importance,
                    "retrievals": 0,
                },
                "losers": [
                    {
                        "id": str(loser.frontmatter.id),
                        "path": str(loser.path),
                        "title": loser.frontmatter.title,
                        "ingested_at": None,
                        "updated": "2026-04-28T12:00:00+00:00",
                        "importance": loser.frontmatter.importance,
                        "retrievals": 0,
                    }
                ],
                "rationale": "test",
                "phase1_qualifies": phase1_qualifies,
            }
        ],
        "rejected": [],
    }


def _write_manifest(tmp_path: Path, payload: dict[str, Any]) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ─── Dry-run is read-only ────────────────────────────────────────────


def test_dry_run_is_read_only(mod: object, vault: Vault, index: Index, tmp_path: Path) -> None:
    winner, loser = _write_pair(vault, index, body="dup body")
    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))

    snapshot: dict[Path, tuple[int, bytes]] = {}
    for f in vault.root.rglob("*.md"):
        snapshot[f] = (f.stat().st_mtime_ns, f.read_bytes())

    report = mod.run(  # type: ignore[attr-defined]
        manifest, vault=vault, index=index, dry_run=True
    )

    assert report.fatal_error is None
    assert report.losers_planned == 1
    assert report.losers_applied == 0
    assert report.audit_rows_written == 0
    for f, (mtime, content) in snapshot.items():
        assert f.stat().st_mtime_ns == mtime
        assert f.read_bytes() == content


# ─── Apply happy path ────────────────────────────────────────────────


def test_apply_only_touches_manifest_losers(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="apply body")
    # Control file: same body, NOT in manifest. Apply must not touch it.
    control_id = str(uuid4())
    control_meta = {
        "id": control_id,
        "type": "memory",
        "created": "2026-04-28T12:00:00+00:00",
        "updated": "2026-04-28T12:00:00+00:00",
        "source": "openclaw",
        "title": "control",
        "provenance": {"source": "openclaw", "ref": "/control.md"},
    }
    control_fm = validate(control_meta)
    control_path = Path(f"memories/{control_id}.md")
    control_memory = Memory(frontmatter=control_fm, body="apply body", path=control_path)
    vault.write(control_memory)
    index.upsert(control_memory)
    control_full = vault.root / control_path
    control_mtime_before = control_full.stat().st_mtime_ns
    control_bytes_before = control_full.read_bytes()

    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))

    report = mod.run(  # type: ignore[attr-defined]
        manifest, vault=vault, index=index, dry_run=False
    )

    assert report.fatal_error is None
    assert report.losers_applied == 1
    assert report.audit_rows_written == 1

    # Loser file got deprecated_by; winner file unchanged in body.
    loser_after = vault.read(loser.path)
    assert loser_after.frontmatter.deprecated_by is not None
    assert str(loser_after.frontmatter.deprecated_by) == str(winner.frontmatter.id)

    winner_after = vault.read(winner.path)
    assert winner_after.frontmatter.deprecated_by is None
    assert winner_after.body == winner.body

    # Control file is bit-identical.
    assert control_full.stat().st_mtime_ns == control_mtime_before
    assert control_full.read_bytes() == control_bytes_before
    control_after = vault.read(control_path)
    assert control_after.frontmatter.deprecated_by is None


def test_audit_row_written_with_phase1_judge(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="audit body")
    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))

    mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]

    rows = index.db.execute(
        "SELECT new_id, existing_id, verdict, judge, applied FROM dedup_audit"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["new_id"] == str(loser.frontmatter.id)
    assert row["existing_id"] == str(winner.frontmatter.id)
    assert row["verdict"] == "DUPLICATE"
    assert row["judge"] == "phase1-manifest"
    assert row["applied"] == 1


# ─── Defense-in-depth: each rule fires correctly ────────────────────


def test_skip_when_loser_hash_drifted(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="initial body")
    manifest_payload = _manifest(winner=winner, loser=loser)
    manifest_path = _write_manifest(tmp_path, manifest_payload)

    # Simulate drift: someone edited the loser body after the audit ran.
    drifted = Memory(
        frontmatter=loser.frontmatter,
        body="initial body  PLUS extra",
        path=loser.path,
    )
    vault.write(drifted)

    report = mod.run(  # type: ignore[attr-defined]
        manifest_path, vault=vault, index=index, dry_run=False
    )

    assert report.losers_applied == 0
    assert report.groups_skipped == 1
    reasons = report.outcomes[0].skip_reasons
    assert any("body hash drifted" in r for r in reasons)


def test_skip_when_loser_already_deprecated(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="already-dep body")
    pre_existing = uuid4()
    new_fm = loser.frontmatter.model_copy(update={"deprecated_by": pre_existing})
    vault.write(Memory(frontmatter=new_fm, body=loser.body, path=loser.path))
    index.upsert(Memory(frontmatter=new_fm, body=loser.body, path=loser.path))

    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]

    assert report.losers_applied == 0
    assert report.groups_skipped == 1
    reasons = report.outcomes[0].skip_reasons
    assert any("already deprecated" in r for r in reasons)
    # The pre-existing pointer must not be overwritten.
    after = vault.read(loser.path)
    assert after.frontmatter.deprecated_by == pre_existing


def test_idempotent_after_apply(mod: object, vault: Vault, index: Index, tmp_path: Path) -> None:
    winner, loser = _write_pair(vault, index, body="idem body")
    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))
    first = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    second = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]

    assert first.losers_applied == 1
    assert second.losers_applied == 0
    assert second.groups_skipped == 1
    reasons = second.outcomes[0].skip_reasons
    assert any("already deprecated" in r for r in reasons)


def test_skip_blocked_type_at_apply_time(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="b body")
    # Manifest claims the type is 'skill' even though files are memory.
    payload = _manifest(winner=winner, loser=loser, type_="skill")
    manifest = _write_manifest(tmp_path, payload)
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    assert report.losers_applied == 0
    assert report.groups_skipped == 1
    reasons = report.outcomes[0].skip_reasons
    assert any("blocked type" in r for r in reasons)


def test_skip_when_phase1_qualifies_false(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="pq body")
    payload = _manifest(winner=winner, loser=loser, phase1_qualifies=False)
    manifest = _write_manifest(tmp_path, payload)
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    assert report.losers_applied == 0
    assert report.groups_skipped == 1


def test_fatal_on_bad_schema_version(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="x")
    payload = _manifest(winner=winner, loser=loser, schema_version=999)
    manifest = _write_manifest(tmp_path, payload)
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    assert report.fatal_error is not None
    assert "schema_version" in report.fatal_error
    assert report.losers_applied == 0


def test_fatal_on_wrong_rule_set(mod: object, vault: Vault, index: Index, tmp_path: Path) -> None:
    winner, loser = _write_pair(vault, index, body="x")
    payload = _manifest(winner=winner, loser=loser, rule_set_version="not-phase1")
    manifest = _write_manifest(tmp_path, payload)
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    assert report.fatal_error is not None
    assert "rule_set_version" in report.fatal_error
    assert report.losers_applied == 0


def test_skip_when_winner_id_does_not_match_file(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="mismatch body")
    payload = _manifest(winner=winner, loser=loser)
    payload["groups"][0]["winner"]["id"] = str(uuid4())
    manifest = _write_manifest(tmp_path, payload)
    report = mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]
    assert report.losers_applied == 0
    reasons = report.outcomes[0].skip_reasons
    assert any("winner id mismatch" in r for r in reasons)


# ─── Blast-radius assertion ──────────────────────────────────────────


def test_apply_does_not_touch_unrelated_files(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    """Hard guarantee: an apply pass must NOT modify any file whose
    path is not in the manifest's loser list, regardless of content
    similarity."""
    winner, loser = _write_pair(vault, index, body="blast body")

    # 5 unrelated files of varying types and bodies, none in manifest.
    unrelated_paths: list[Path] = []
    for i in range(5):
        rid = str(uuid4())
        unrelated_meta: dict[str, Any] = {
            "id": rid,
            "type": "memory",
            "created": "2026-04-28T12:00:00+00:00",
            "updated": "2026-04-28T12:00:00+00:00",
            "source": "openclaw",
            "title": f"u{i}",
            "provenance": {"source": "openclaw", "ref": f"/u{i}.md"},
        }
        unrelated_fm = validate(unrelated_meta)
        rel = Path(f"memories/{rid}.md")
        body = "blast body" if i == 0 else f"unrelated body {i}"
        unrelated = Memory(frontmatter=unrelated_fm, body=body, path=rel)
        vault.write(unrelated)
        index.upsert(unrelated)
        unrelated_paths.append(vault.root / rel)

    snapshot: dict[Path, tuple[int, bytes]] = {
        p: (p.stat().st_mtime_ns, p.read_bytes()) for p in unrelated_paths
    }

    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))
    mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]

    for p, (mtime, content) in snapshot.items():
        assert p.stat().st_mtime_ns == mtime, f"unrelated file mutated: {p}"
        assert p.read_bytes() == content, f"unrelated file content changed: {p}"


def test_no_extra_audit_rows_for_unrelated(
    mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    winner, loser = _write_pair(vault, index, body="audit-iso")
    # Pre-existing audit rows from another judge.
    index.db.execute(
        """INSERT INTO dedup_audit (ts, new_id, existing_id, verdict, rationale, judge, applied)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(tz=UTC).isoformat(),
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            "DUPLICATE",
            "test pre-existing row",
            "other-judge",
            1,
        ),
    )
    index.db.commit()
    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))
    mod.run(manifest, vault=vault, index=index, dry_run=False)  # type: ignore[attr-defined]

    # We should have exactly one new phase1-manifest row.
    n_phase1 = index.db.execute(
        "SELECT COUNT(*) FROM dedup_audit WHERE judge = ?", ("phase1-manifest",)
    ).fetchone()[0]
    assert n_phase1 == 1
    # The pre-existing 'other-judge' row is untouched.
    n_other = index.db.execute(
        "SELECT COUNT(*) FROM dedup_audit WHERE judge = ?", ("other-judge",)
    ).fetchone()[0]
    assert n_other == 1


# ─── No --apply default ──────────────────────────────────────────────


def test_main_default_is_dry_run(mod: object, vault: Vault, index: Index, tmp_path: Path) -> None:
    """Running ``main(["--manifest", ...])`` without --apply must
    not mutate the vault."""
    # Note: `main` opens its own Index instance, so we close ours
    # first to avoid SQLite locking on the shared DB file.
    winner, loser = _write_pair(vault, index, body="cli-dry")
    manifest = _write_manifest(tmp_path, _manifest(winner=winner, loser=loser))
    snapshot: dict[Path, tuple[int, bytes]] = {
        f: (f.stat().st_mtime_ns, f.read_bytes()) for f in vault.root.rglob("*.md")
    }
    index.close()
    rc = mod.main(  # type: ignore[attr-defined]
        ["--manifest", str(manifest), "--vault", str(vault.root)]
    )
    assert rc == 0
    for f, (mtime, content) in snapshot.items():
        assert f.stat().st_mtime_ns == mtime
        assert f.read_bytes() == content


# ─── Helpers ─────────────────────────────────────────────────────────


def _row_count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
