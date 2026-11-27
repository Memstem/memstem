"""Tests for lifting expiries set by retired noise-filter rules (ADR 0058)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault
from memstem.hygiene.noise_expiry import apply_lift, find_liftable

_EXPIRED = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


def _save(vault: Vault, index: Index, body: str, ref: str, *, source: str = "claude-code") -> str:
    """Store ``body`` expired the way pre-ADR-0058 code left it (no stamp).

    Ingests a neutral placeholder (noise bodies would be dropped), then writes
    the real body and the expiry straight to the vault and index.
    """
    memory = Pipeline(vault, index).process(
        MemoryRecord(
            source=source,
            ref=ref,
            title=body[:40],
            body=f"placeholder for {ref}",
            tags=[],
            metadata={
                "type": "session",
                "created": "2026-09-28T10:00:00+00:00",
                "session_id": "2c6f0a93-8e41-4b7d-9f25-d1a8b3e60c74",
            },
        )
    )
    assert memory is not None
    fm = memory.frontmatter.model_copy(update={"valid_to": _EXPIRED})
    expired = Memory(frontmatter=fm, body=body, path=memory.path)
    vault.write(expired)
    index.upsert(expired)
    index.db.execute("DELETE FROM embed_queue")
    return str(memory.id)


def test_lifts_old_transient_task_expiry_and_requeues(vault: Vault, index: Index) -> None:
    mid = _save(
        vault,
        index,
        "**User:** where is the shipping portal?\n\n**Assistant:** I'll merge the fix by tomorrow.",
        "/s/1.jsonl",
    )
    plan = find_liftable(vault, index)
    assert [(h.id, h.old_rule) for h in plan.hits] == [(mid, "transient_task")]
    assert apply_lift(vault, index, plan) == 1
    row = index.db.execute("SELECT valid_to FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row[0] is None
    assert vault.read(plan.hits[0].path).frontmatter.valid_to is None
    queued = index.db.execute(
        "SELECT COUNT(*) FROM embed_queue WHERE memory_id = ?", (mid,)
    ).fetchone()[0]
    assert queued == 1
    # Idempotent: nothing left to lift.
    assert find_liftable(vault, index).hits == ()


def test_leaves_expiry_on_record_matching_no_old_rule(vault: Vault, index: Index) -> None:
    _save(vault, index, "A decision about the Cloudflare migration.", "/s/2.jsonl")
    assert find_liftable(vault, index).hits == ()


def test_leaves_expiry_the_current_filter_still_sets(vault: Vault, index: Index) -> None:
    body = (
        "**User:** [cron:7c4e19b2-3a5f-4d81-9e6c-0b2f8a5d1e47 Inbox sweep] run it, "
        "and deploy the digest by Friday.\n\n**Assistant:** done"
    )
    _save(vault, index, body, "/x.sqlite#session=a", source="openclaw")
    plan = find_liftable(vault, index, openclaw_scheduled_session_ttl_days=28)
    assert plan.hits == ()


def test_leaves_genuine_heartbeat_noise(vault: Vault, index: Index) -> None:
    _save(vault, index, "[heartbeat] ok\nHEARTBEAT_OK\nAll checks passed.", "/m/hb.md")
    assert find_liftable(vault, index).hits == ()
