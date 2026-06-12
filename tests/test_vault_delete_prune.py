"""Vault-delete prune: index rows must not outlive their markdown file.

The vault is canonical and the index is derived — but nothing used to
notice a deleted vault file, so its memories/FTS/vec rows kept serving
search results for content that no longer existed. The reconcile-time
sweep (`_prune_deleted_vault_files`) and the embed worker's
missing-file path both prune now.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.cli import _prune_deleted_vault_files
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault


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


def _ingest(pipe: Pipeline, body: str) -> Memory:
    record = MemoryRecord(
        source="test",
        ref=f"/tmp/{uuid4()}.md",
        title="t",
        body=body,
        tags=[],
        metadata={
            "type": "memory",
            "created": "2026-04-26T00:00:00+00:00",
            "updated": "2026-04-26T00:00:00+00:00",
        },
    )
    memory = pipe.process(record)
    assert memory is not None, "pipeline unexpectedly noise-filtered the test record"
    return memory


def _row_counts(index: Index, memory_id: str) -> dict[str, int]:
    counts = {}
    for table, col in [
        ("memories", "id"),
        ("memories_fts", "memory_id"),
        ("embed_queue", "memory_id"),
        ("embed_state", "memory_id"),
    ]:
        counts[table] = index.db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (memory_id,)
        ).fetchone()["n"]
    return counts


class TestPruneSweep:
    def test_prunes_rows_for_deleted_file(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        gone = _ingest(pipe, "distinct body that will be deleted")
        kept = _ingest(pipe, "distinct body that stays")
        (vault.root / gone.path).unlink()

        assert _prune_deleted_vault_files(vault, index) == 1
        assert all(n == 0 for n in _row_counts(index, str(gone.id)).values())
        counts = _row_counts(index, str(kept.id))
        assert counts["memories"] == 1
        assert counts["memories_fts"] == 1
        assert counts["embed_queue"] == 1  # still awaiting its embed

    def test_noop_when_vault_intact(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        _ingest(pipe, "a perfectly healthy record body")
        assert _prune_deleted_vault_files(vault, index) == 0
        assert index.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1

    def test_safety_valve_skips_mass_missing(self, vault: Vault, index: Index) -> None:
        """When most of the vault is 'missing', the likely cause is a
        bad mount, not deletions — the automatic sweep must refuse to
        wipe the index."""
        pipe = Pipeline(vault, index)
        memories = [_ingest(pipe, f"unique throwaway body number {i}") for i in range(12)]
        for m in memories:
            (vault.root / m.path).unlink()

        assert _prune_deleted_vault_files(vault, index) == 0
        assert index.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 12

    def test_reindex_mode_prunes_unconditionally(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memories = [_ingest(pipe, f"unique throwaway body number {i}") for i in range(12)]
        for m in memories:
            (vault.root / m.path).unlink()

        assert _prune_deleted_vault_files(vault, index, max_fraction=None) == 12
        assert index.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0

    def test_small_vault_below_floor_still_prunes(self, vault: Vault, index: Index) -> None:
        """The safety valve has a 10-row floor so small vaults keep
        auto-pruning — 3 deletions out of 3 records is plausibly real."""
        pipe = Pipeline(vault, index)
        memories = [_ingest(pipe, f"unique small-vault body {i}") for i in range(3)]
        for m in memories:
            (vault.root / m.path).unlink()

        assert _prune_deleted_vault_files(vault, index) == 3
        assert index.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0
