"""Tests for ``memstem.hygiene.expired_vectors`` and ``Index.strip_vectors``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault
from memstem.hygiene.expired_vectors import apply_strip, find_expired_with_vectors

DIMS = 8


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "sessions", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=DIMS)
    idx.connect()
    yield idx
    idx.close()


def _write(vault: Vault, index: Index, *, valid_to: str | None, type_: str = "session") -> Memory:
    meta: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "openclaw",
        "title": "t",
    }
    if valid_to:
        meta["valid_to"] = valid_to
    fm = validate(meta)
    folder = "sessions" if type_ == "session" else "memories"
    memory = Memory(frontmatter=fm, body="body text", path=Path(f"{folder}/{fm.id}.md"))
    vault.write(memory)
    index.upsert(memory)
    index.upsert_vectors(str(fm.id), ["c0", "c1"], [[0.1] * DIMS, [0.2] * DIMS])
    index.record_embed_state(str(fm.id), "hash", "sig")
    return memory


def test_finds_only_expired_records_with_vectors(vault: Vault, index: Index) -> None:
    expired = _write(vault, index, valid_to="2026-05-01T00:00:00+00:00")
    _write(vault, index, valid_to="2099-01-01T00:00:00+00:00")  # not yet expired
    _write(vault, index, valid_to=None)  # never expires
    plan = find_expired_with_vectors(index, now=datetime(2026, 10, 3, tzinfo=UTC))
    assert [h.id for h in plan.hits] == [str(expired.frontmatter.id)]
    assert plan.chunks == 2


def test_type_filter(vault: Vault, index: Index) -> None:
    _write(vault, index, valid_to="2026-05-01T00:00:00+00:00", type_="memory")
    plan = find_expired_with_vectors(index, types=frozenset({"session"}))
    assert plan.hits == ()


def test_strip_keeps_row_fts_and_embed_state(vault: Vault, index: Index) -> None:
    mem = _write(vault, index, valid_to="2026-05-01T00:00:00+00:00")
    mid = str(mem.frontmatter.id)
    plan = find_expired_with_vectors(index)
    assert apply_strip(index, plan) == 2
    assert index._vec_chunk_ids(mid) == []
    assert index.db.execute("SELECT 1 FROM memories WHERE id = ?", (mid,)).fetchone()
    assert index.db.execute("SELECT 1 FROM memories_fts WHERE memory_id = ?", (mid,)).fetchone()
    # embed_state survives, so the record is not treated as "never embedded"
    assert not index.needs_reembed(mid, "hash", "sig")
    # idempotent: nothing left to strip
    assert find_expired_with_vectors(index).hits == ()


def test_pipeline_does_not_enqueue_expired_record(vault: Vault, index: Index) -> None:
    pipe = Pipeline(vault, index, openclaw_scheduled_session_ttl_days=28)
    rec = MemoryRecord(
        source="openclaw",
        ref="/x.sqlite#session=s1",
        title="[cron:…]",
        body=(
            "**User:** [cron:d23aa873-9cd3-49de-ba33-314a9f1e8cad Ari Full Heartbeat] run.\n\n"
            "**Assistant:** ok"
        ),
        metadata={"type": "session", "created": "2026-04-27T10:00:00+00:00", "session_id": "s1"},
    )
    memory = pipe.process(rec)
    assert memory is not None and memory.frontmatter.valid_to is not None
    queued = index.db.execute(
        "SELECT 1 FROM embed_queue WHERE memory_id = ?", (str(memory.frontmatter.id),)
    ).fetchone()
    assert queued is None
