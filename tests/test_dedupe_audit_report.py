"""Tests for ``scripts/dedupe_audit_report.py``.

The audit script is the primary mutation-free interface for surveying a
vault's duplicates. These tests pin two invariants:

1. Functional: classes A/B/C/D/E/G fire on the right synthetic
   inputs.
2. Safety: running the audit cannot mutate the vault or the index.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_audit_report.py"


def _load_audit_module() -> object:
    spec = importlib.util.spec_from_file_location("dedupe_audit_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass annotation evaluation can find
    # the module in sys.modules — required for `dict[str, Any]` field
    # types declared inside the script.
    sys.modules["dedupe_audit_report"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audit_mod() -> object:
    return _load_audit_module()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "vault" / "_meta" / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


def _write(
    vault: Vault,
    index: Index,
    *,
    title: str,
    body: str,
    type_: str = "memory",
    provenance_ref: str | None = None,
    importance: float | None = None,
    updated: str = "2026-04-28T12:00:00+00:00",
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-28T12:00:00+00:00",
        "updated": updated,
        "source": "test",
        "title": title,
    }
    if provenance_ref:
        metadata["provenance"] = {"source": "test", "ref": provenance_ref}
    if importance is not None:
        metadata["importance"] = importance
    if type_ == "skill":
        metadata["scope"] = "universal"
        metadata["verification"] = "verify by hand"
    fm = validate(metadata)
    if type_ == "skill":
        slug = title.lower().replace(" ", "-")
        path = Path(f"skills/{slug}-{fm.id}.md")
    elif type_ == "session":
        path = Path(f"sessions/{fm.id}.md")
    elif type_ == "daily":
        path = Path(f"daily/{fm.id}.md")
    else:
        path = Path(f"memories/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    index.upsert(memory)
    return memory


# ─── Functional ──────────────────────────────────────────────────────


def test_classes_a_and_b_fire_on_reingest(audit_mod: object, vault: Vault, index: Index) -> None:
    _write(
        vault,
        index,
        title="People — Extended Context",
        body="A reingest body that ought to collide on hash.",
        provenance_ref="/tmp/source/people.md",
    )
    _write(
        vault,
        index,
        title="People — Extended Context",
        body="A reingest body that ought to collide on hash.",
        provenance_ref="/tmp/source/people.md",
    )
    _write(vault, index, title="Unrelated", body="Different content.")

    views = audit_mod.collect_views(vault, index)  # type: ignore[attr-defined]
    groups = audit_mod.classify(views)  # type: ignore[attr-defined]

    classes = {g.klass for g in groups}
    assert "A" in classes  # body-hash collision
    assert "B" in classes  # confirming provenance.ref match
    assert "D" in classes  # confirming title match


def test_class_c_keep_all_for_source_updated(audit_mod: object, vault: Vault, index: Index) -> None:
    _write(
        vault,
        index,
        title="Hard Rules",
        body="version one of the rules",
        provenance_ref="/tmp/source/RULES.md",
    )
    _write(
        vault,
        index,
        title="Hard Rules",
        body="version two of the rules — edited later",
        provenance_ref="/tmp/source/RULES.md",
    )

    views = audit_mod.collect_views(vault, index)  # type: ignore[attr-defined]
    groups = audit_mod.classify(views)  # type: ignore[attr-defined]

    c_groups = [g for g in groups if g.klass == "C"]
    assert len(c_groups) == 1
    assert c_groups[0].recommended_action == "keep_all"
    assert c_groups[0].risk == "high"


def test_class_g_skill_collision_never_quarantine(
    audit_mod: object, vault: Vault, index: Index
) -> None:
    body = "Identical skill body that lives in two places."
    _write(vault, index, title="SEO Optimizer A", body=body, type_="skill")
    _write(vault, index, title="SEO Optimizer B", body=body, type_="skill")

    views = audit_mod.collect_views(vault, index)  # type: ignore[attr-defined]
    groups = audit_mod.classify(views)  # type: ignore[attr-defined]

    g_groups = [g for g in groups if g.klass == "G"]
    assert len(g_groups) == 1
    assert g_groups[0].recommended_action == "manual_review"
    assert g_groups[0].risk == "very_high"
    # Skill-involved groups must not propose a quarantine winner.
    assert g_groups[0].winner_id is None


def test_class_e_title_match_different_bodies(
    audit_mod: object, vault: Vault, index: Index
) -> None:
    _write(vault, index, title="Status update", body="Body one.")
    _write(vault, index, title="Status update", body="Body two — different.")

    views = audit_mod.collect_views(vault, index)  # type: ignore[attr-defined]
    groups = audit_mod.classify(views)  # type: ignore[attr-defined]

    e_groups = [g for g in groups if g.klass == "E"]
    assert len(e_groups) == 1
    assert e_groups[0].recommended_action == "manual_review"
    assert e_groups[0].confidence == "low"


def test_already_deprecated_records_excluded(audit_mod: object, vault: Vault, index: Index) -> None:
    a = _write(vault, index, title="Same body", body="X")
    _write(vault, index, title="Same body", body="X")
    # Mark `a` already deprecated. Audit must skip its group entirely.
    fm = a.frontmatter.model_copy(update={"deprecated_by": uuid4()})
    vault.write(Memory(frontmatter=fm, body=a.body, path=a.path))
    index.upsert(Memory(frontmatter=fm, body=a.body, path=a.path))

    views = audit_mod.collect_views(vault, index)  # type: ignore[attr-defined]
    groups = audit_mod.classify(views)  # type: ignore[attr-defined]
    # Only one non-deprecated record left → no candidate group.
    assert not [g for g in groups if g.klass in {"A", "B", "D"}]


# ─── Safety: read-only invariant ─────────────────────────────────────


def test_audit_does_not_mutate_vault_or_index(
    audit_mod: object, vault: Vault, index: Index, tmp_path: Path
) -> None:
    """Running the audit must leave every vault file and index row unchanged."""
    _write(
        vault,
        index,
        title="Reingest A",
        body="Same body.",
        provenance_ref="/x.md",
    )
    _write(
        vault,
        index,
        title="Reingest A",
        body="Same body.",
        provenance_ref="/x.md",
    )
    _write(vault, index, title="Skill conflict", body="S", type_="skill")
    _write(vault, index, title="Skill conflict 2", body="S", type_="skill")

    # Snapshot file mtimes + contents.
    snapshot: dict[Path, tuple[int, bytes]] = {}
    for f in vault.root.rglob("*.md"):
        snapshot[f] = (f.stat().st_mtime_ns, f.read_bytes())

    # Snapshot index row counts for the tables the audit touches.
    db_path = vault.root / "_meta" / "index.db"
    # Make sure the audit reads the same DB the test fixture wrote to.
    # The fixture's index already lives there; we just snapshot counts.
    counts_before = _table_counts(index.db)

    out_dir = tmp_path / "audit-out"
    out_dir.mkdir()
    rc = audit_mod.main(  # type: ignore[attr-defined]
        ["--vault", str(vault.root), "--out-dir", str(out_dir)]
    )
    assert rc == 0

    # Vault files unchanged (mtime + bytes).
    for f, (mtime, content) in snapshot.items():
        assert f.stat().st_mtime_ns == mtime, f"audit mutated mtime of {f}"
        assert f.read_bytes() == content, f"audit mutated content of {f}"

    # Index row counts unchanged for every table that already existed.
    counts_after = _table_counts(_open_ro(db_path))
    for table, n in counts_before.items():
        assert counts_after.get(table) == n, (
            f"audit changed row count for {table}: {n} -> {counts_after.get(table)}"
        )

    # Output artifacts present and parseable.
    md_reports = list(out_dir.glob("dedupe-audit-*.md"))
    json_reports = list(out_dir.glob("dedupe-audit-*.json"))
    assert len(md_reports) == 1
    assert len(json_reports) == 1
    payload = json.loads(json_reports[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["totals"]["records"] >= 4


def test_script_module_imports_no_writers(audit_mod: object) -> None:
    """Belt-and-suspenders: the script's namespace should not expose
    any mutating function from cleanup_retro."""
    # These are the writer functions in cleanup_retro that the audit
    # must never call. Importing them by name into the audit module
    # would be a sign the read-only contract is loosening.
    forbidden = {
        "apply_dedup_collisions",
        "apply_noise_expiry",
        "write_skill_review_ticket",
    }
    assert not (forbidden & set(dir(audit_mod)))


# ─── Helpers ─────────────────────────────────────────────────────────


def _table_counts(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memories_fts%' "
        "AND name NOT LIKE 'memories_vec%'"
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[0]
        n = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
        out[name] = int(n[0])
    return out


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
