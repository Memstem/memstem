"""ADR 0046: removing Claude Code subagent-transcript records left by <0.25.1."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.cli import _prune_subagent_records
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault

ROOT = "/home/u/.claude/projects/-home-u"
SID = "0c052502-2041-481d-9713-c84abda649ff"


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


def _session(ref: str, body: str) -> MemoryRecord:
    return MemoryRecord(
        source="claude-code",
        ref=ref,
        title="t",
        body=body,
        tags=[],
        metadata={
            "type": "session",
            "session_id": SID,
            "created": "2026-08-27T00:00:00+00:00",
            "updated": "2026-08-27T00:00:00+00:00",
        },
    )


def test_subagent_holding_parent_path_is_removed(vault: Vault, index: Index) -> None:
    pipe = Pipeline(vault, index)
    sub_ref = f"{ROOT}/{SID}/subagents/agent-a1.jsonl"
    memory = pipe.process(_session(sub_ref, "subagent transcript"))
    assert memory is not None and str(memory.path) == f"sessions/{SID}.md"

    assert _prune_subagent_records(vault, index) == 1
    assert index.get_path(str(memory.id)) is None
    assert not (vault.root / memory.path).exists()
    assert index.lookup_record_mapping("claude-code", sub_ref) is None
    assert _prune_subagent_records(vault, index) == 0  # idempotent


def test_parent_that_reclaimed_the_path_is_untouched(vault: Vault, index: Index) -> None:
    pipe = Pipeline(vault, index)
    sub_ref = f"{ROOT}/{SID}/subagents/agent-a1.jsonl"
    pipe.process(_session(sub_ref, "subagent transcript"))
    parent = pipe.process(_session(f"{ROOT}/{SID}.jsonl", "parent transcript"))
    assert parent is not None  # displaced the subagent record at the same path

    assert _prune_subagent_records(vault, index) == 0
    assert index.get_path(str(parent.id)) == f"sessions/{SID}.md"
    assert (vault.root / f"sessions/{SID}.md").read_text().rstrip().endswith("parent transcript")
    assert index.lookup_record_mapping("claude-code", sub_ref) is None


def test_non_subagent_records_are_ignored(vault: Vault, index: Index) -> None:
    pipe = Pipeline(vault, index)
    memory = pipe.process(_session(f"{ROOT}/{SID}.jsonl", "parent transcript"))
    assert memory is not None
    assert _prune_subagent_records(vault, index) == 0
    assert index.get_path(str(memory.id)) is not None
