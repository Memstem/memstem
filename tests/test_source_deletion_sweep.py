"""Source-deletion tombstone sweep (ADR 0026).

When a user deletes an *authored* source file (memory/skill/daily) in their
agent workspace, MemStem must mark the corresponding memory ``deleted_at`` so
it drops out of search — while leaving session logs and the distillations
derived from them untouched, never mass-tombstoning a vanished mount, and
restoring a memory whose source comes back.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.cli import _sweep_deleted_sources
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Memory, Vault


class FakeAdapter(Adapter):
    """Minimal adapter; inherits the default file-backed ``source_exists``.

    Optionally declares ``source_roots`` so the sweep's root-liveness guard and
    per-root safety valve can be exercised (ADR 0026)."""

    name = "test"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = list(roots) if roots else []

    def source_roots(self) -> list[Path]:
        return self._roots

    def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:  # pragma: no cover
        raise NotImplementedError

    def reconcile(
        self, paths: list[Path]
    ) -> AsyncGenerator[MemoryRecord, None]:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=8)
    idx.connect()
    yield idx
    idx.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def adapters() -> dict[str, Adapter]:
    return {"test": FakeAdapter()}


def _ingest(
    pipe: Pipeline,
    workspace: Path,
    name: str,
    body: str,
    *,
    mtype: str = "memory",
    extra: dict[str, object] | None = None,
) -> tuple[Memory | None, Path]:
    """Create a real source file under the workspace and ingest a record for it."""
    src = workspace / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body)
    meta: dict[str, object] = {
        "type": mtype,
        "created": "2026-04-26T00:00:00+00:00",
        "updated": "2026-04-26T00:00:00+00:00",
    }
    if extra:
        meta.update(extra)
    record = MemoryRecord(
        source="test",
        ref=str(src),
        title="t",
        body=body,
        tags=[],
        metadata=meta,
    )
    return pipe.process(record), src


def _deleted_at(vault: Vault, memory: Memory) -> str | None:
    fm = vault.read(memory.path).frontmatter
    return fm.deleted_at.isoformat() if fm.deleted_at else None


def _record_map_count(index: Index, ref: str) -> int:
    return index.db.execute(
        "SELECT COUNT(*) AS n FROM record_map WHERE ref = ?", (ref,)
    ).fetchone()["n"]


class TestSweep:
    def test_tombstones_deleted_authored_file(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        gone, gone_src = _ingest(pipe, workspace, "gone.md", "a memory the user will delete")
        kept, _ = _ingest(pipe, workspace, "kept.md", "a memory the user keeps")
        assert gone is not None and kept is not None

        gone_src.unlink()  # user deletes the source file
        assert _sweep_deleted_sources(vault, index, adapters) == 1

        assert _deleted_at(vault, gone) is not None  # tombstoned
        assert _deleted_at(vault, kept) is None  # untouched
        assert _record_map_count(index, str(gone_src)) == 0  # dead ref pruned

    def test_no_tombstone_when_vault_intact(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        _ingest(pipe, workspace, "present.md", "a healthy memory body")
        assert _sweep_deleted_sources(vault, index, adapters) == 0

    def test_daily_logs_are_swept(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        daily, daily_src = _ingest(
            pipe, workspace, "2026-04-26.md", "daily note body", mtype="daily"
        )
        assert daily is not None
        daily_src.unlink()
        assert _sweep_deleted_sources(vault, index, adapters) == 1
        assert _deleted_at(vault, daily) is not None

    def test_session_logs_are_never_tombstoned(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        """The type filter — not the ref shape — protects sessions. A session
        ``.jsonl`` ref is just as file-backed as a memory, so deleting it must
        NOT tombstone the session record (its distillations stay valid)."""
        pipe = Pipeline(vault, index)
        session, session_src = _ingest(
            pipe, workspace, "session.jsonl", "raw session transcript", mtype="session"
        )
        assert session is not None
        session_src.unlink()
        assert _sweep_deleted_sources(vault, index, adapters) == 0
        assert _deleted_at(vault, session) is None

    def test_multi_ref_not_tombstoned_while_a_sibling_lives(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        """Two identical sources dedup to one memory; deleting the canonical
        one must NOT hide content while an identical source still exists
        (ADR 0026 §4 — duplicates record a ref too)."""
        pipe = Pipeline(vault, index)
        canonical, canonical_src = _ingest(pipe, workspace, "a.md", "identical shared body text")
        dup_result, dup_src = _ingest(pipe, workspace, "b.md", "identical shared body text")
        assert canonical is not None
        assert dup_result is None  # deduped onto the canonical id
        # Both refs are tracked against the canonical memory.
        assert _record_map_count(index, str(canonical_src)) == 1
        assert _record_map_count(index, str(dup_src)) == 1

        canonical_src.unlink()  # delete the canonical source, keep the duplicate
        assert _sweep_deleted_sources(vault, index, adapters) == 0
        assert _deleted_at(vault, canonical) is None  # still visible — dup is alive
        assert _record_map_count(index, str(canonical_src)) == 0  # dead ref pruned
        assert _record_map_count(index, str(dup_src)) == 1  # live ref kept

        dup_src.unlink()  # now delete the last source
        assert _sweep_deleted_sources(vault, index, adapters) == 1
        assert _deleted_at(vault, canonical) is not None

    def test_restore_clears_tombstone(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        mem, src = _ingest(pipe, workspace, "note.md", "a memory that gets deleted then restored")
        assert mem is not None
        src.unlink()
        assert _sweep_deleted_sources(vault, index, adapters) == 1
        assert _deleted_at(vault, mem) is not None

        # User re-creates the file and it re-ingests (same body → dedup path
        # re-establishes the record_map row); the sweep then restores it.
        _ingest(pipe, workspace, "note.md", "a memory that gets deleted then restored")
        _sweep_deleted_sources(vault, index, adapters)
        assert _deleted_at(vault, mem) is None

    def test_declared_root_vanished_is_skipped(
        self, vault: Vault, index: Index, tmp_path: Path
    ) -> None:
        """When a declared workspace root disappears entirely (unmount/move),
        every ref under it is skipped — never mass-tombstoned."""
        pipe = Pipeline(vault, index)
        root = tmp_path / "workspace_root"
        root.mkdir()
        mem, _ = _ingest(pipe, root, "note.md", "lives in a workspace that disappears")
        assert mem is not None
        adapters: dict[str, Adapter] = {"test": FakeAdapter(roots=[root])}

        import shutil

        shutil.rmtree(root)  # the whole declared root is gone
        assert _sweep_deleted_sources(vault, index, adapters) == 0
        assert _deleted_at(vault, mem) is None

    def test_subdir_deletion_under_live_root_tombstones(
        self, vault: Vault, index: Index, tmp_path: Path
    ) -> None:
        """Deleting a whole sub-folder of notes while the workspace root still
        exists is a real deletion — those notes must be tombstoned (the v2 bug
        was that a missing parent dir wrongly looked like an unmounted root)."""
        pipe = Pipeline(vault, index)
        root = tmp_path / "workspace_root"
        sub = root / "project_x"
        sub.mkdir(parents=True)
        mem, _ = _ingest(pipe, sub, "note.md", "note inside a project subfolder")
        assert mem is not None
        adapters: dict[str, Adapter] = {"test": FakeAdapter(roots=[root])}

        import shutil

        shutil.rmtree(sub)  # delete the subfolder; root survives
        assert root.is_dir()
        assert _sweep_deleted_sources(vault, index, adapters) == 1
        assert _deleted_at(vault, mem) is not None

    def test_no_declared_root_falls_back_to_parent_dir(
        self, vault: Vault, index: Index, tmp_path: Path, adapters: dict[str, Adapter]
    ) -> None:
        """Adapters that declare no roots use the containing-directory fallback:
        a vanished parent dir is treated conservatively as a missing root."""
        pipe = Pipeline(vault, index)
        sub = tmp_path / "undeclared"
        sub.mkdir()
        mem, src = _ingest(pipe, sub, "note.md", "no declared root for this one")
        assert mem is not None
        src.unlink()
        sub.rmdir()
        assert _sweep_deleted_sources(vault, index, adapters) == 0  # fallback skip
        assert _deleted_at(vault, mem) is None

    def test_per_root_valve_isolation(self, vault: Vault, index: Index, tmp_path: Path) -> None:
        """The safety valve is per-root: a mass disappearance in one live root
        is blocked, while a normal single deletion in a SEPARATE root under the
        same adapter still tombstones (the v2 bug pooled by adapter name)."""
        pipe = Pipeline(vault, index)
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        a_mems = [_ingest(pipe, root_a, f"a{i}.md", f"root-a body {i}")[0] for i in range(12)]
        b_mem, b_src = _ingest(pipe, root_b, "b.md", "root-b body that is deleted")
        assert b_mem is not None
        adapters: dict[str, Adapter] = {"test": FakeAdapter(roots=[root_a, root_b])}

        # Mass-delete root_a's files (dirs intact → not a vanished root, trips
        # the per-root fraction valve) and a single file in root_b.
        for i in range(12):
            (root_a / f"a{i}.md").unlink()
        b_src.unlink()

        tombstoned = _sweep_deleted_sources(vault, index, adapters)
        assert tombstoned == 1  # only root_b's single deletion
        assert _deleted_at(vault, b_mem) is not None
        for m in a_mems:  # root_a's mass-missing batch was valve-blocked
            assert m is not None
            assert _deleted_at(vault, m) is None

    def test_cross_type_body_collision_does_not_attach_session_ref(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        """A session whose body collides (dedup) with an authored memory must
        NOT get its ref attached to the memory — else rotating the .jsonl would
        look like a dead authored source and wrongly tombstone the memory."""
        pipe = Pipeline(vault, index)
        shared_body = "identical body shared by a memory and a session log"
        mem, _ = _ingest(pipe, workspace, "note.md", shared_body, mtype="memory")
        assert mem is not None
        # A session with the same body dedups onto the memory; its ref must not
        # be recorded against the authored memory.
        session_res, session_src = _ingest(pipe, workspace, "s.jsonl", shared_body, mtype="session")
        assert session_res is None  # deduped
        assert _record_map_count(index, str(session_src)) == 0  # ref NOT attached

        session_src.unlink()  # rotate/delete the session log
        assert _sweep_deleted_sources(vault, index, adapters) == 0
        assert _deleted_at(vault, mem) is None  # memory untouched

    def test_safety_valve_skips_mass_missing(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        """When most of a source's authored files vanish at once (parent dirs
        intact), it's a bad mount — the per-source valve refuses to mass-hide."""
        pipe = Pipeline(vault, index)
        mems = [
            _ingest(pipe, workspace, f"n{i}.md", f"unique body number {i}")[0] for i in range(12)
        ]
        for i in range(12):
            (workspace / f"n{i}.md").unlink()
        assert _sweep_deleted_sources(vault, index, adapters) == 0
        for m in mems:
            assert m is not None
            assert _deleted_at(vault, m) is None

    def test_force_mode_tombstones_unconditionally(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        mems = [
            _ingest(pipe, workspace, f"n{i}.md", f"unique body number {i}")[0] for i in range(12)
        ]
        for i in range(12):
            (workspace / f"n{i}.md").unlink()
        assert _sweep_deleted_sources(vault, index, adapters, max_fraction=None) == 12
        for m in mems:
            assert m is not None
            assert _deleted_at(vault, m) is not None

    def test_orphan_record_map_rows_cleaned(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        """A record_map row whose memory was hard-pruned must be removed."""
        pipe = Pipeline(vault, index)
        mem, src = _ingest(pipe, workspace, "note.md", "memory whose index row gets pruned")
        assert mem is not None
        index.delete(str(mem.id))  # hard-delete the memory row, leaving record_map dangling
        assert _record_map_count(index, str(src)) == 1
        _sweep_deleted_sources(vault, index, adapters)
        assert _record_map_count(index, str(src)) == 0


class TestSearchFiltersDeleted:
    def test_deleted_excluded_by_default_and_surfaced_with_flag(
        self, vault: Vault, index: Index, workspace: Path, adapters: dict[str, Adapter]
    ) -> None:
        pipe = Pipeline(vault, index)
        mem, src = _ingest(
            pipe, workspace, "needle.md", "the unmistakable zorptastic needle phrase"
        )
        assert mem is not None
        search = Search(vault=vault, index=index, embedder=None)

        before = search.search("zorptastic needle", limit=5)
        assert any(r.memory.frontmatter.id == mem.id for r in before)

        src.unlink()
        assert _sweep_deleted_sources(vault, index, adapters) == 1

        after = search.search("zorptastic needle", limit=5)
        assert all(r.memory.frontmatter.id != mem.id for r in after)

        audit = search.search("zorptastic needle", limit=5, include_deleted=True)
        assert any(r.memory.frontmatter.id == mem.id for r in audit)
