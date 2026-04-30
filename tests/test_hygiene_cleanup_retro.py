"""Tests for ``memstem.hygiene.cleanup_retro``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.search import Search
from memstem.core.storage import Memory, Vault
from memstem.hygiene.cleanup_retro import (
    SKILL_REVIEW_DIRNAME,
    CollisionMember,
    apply_dedup_collisions,
    apply_noise_expiry,
    find_dedup_collisions,
    find_noise_hits,
    format_dedup_report,
    format_noise_report,
    select_winner,
    write_skill_review_ticket,
)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


def _write_memory(
    vault: Vault,
    index: Index,
    *,
    title: str,
    body: str,
    type_: str = "memory",
    importance: float | None = None,
    updated: str = "2026-04-25T15:00:00+00:00",
) -> Memory:
    """Write a minimal memory directly through the vault + index."""
    from uuid import uuid4

    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": updated,
        "source": "test",
        "title": title,
    }
    if importance is not None:
        metadata["importance"] = importance
    if type_ == "skill":
        metadata["scope"] = "universal"
        metadata["verification"] = "verify by hand"
    fm = validate(metadata)
    if type_ == "skill":
        path = Path(f"skills/{title.lower().replace(' ', '-')}.md")
    else:
        path = Path(f"memories/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    index.upsert(memory)
    return memory


# ─── select_winner ──────────────────────────────────────────────────


def test_select_winner_picks_highest_importance() -> None:
    members = (
        CollisionMember(
            id="a",
            type="memory",
            title="A",
            path="memories/a.md",
            importance=0.4,
            retrievals=0,
            updated=None,
        ),
        CollisionMember(
            id="b",
            type="memory",
            title="B",
            path="memories/b.md",
            importance=0.9,
            retrievals=0,
            updated=None,
        ),
    )
    from memstem.hygiene.cleanup_retro import CollisionGroup

    group = CollisionGroup(body_hash="h", members=members)
    winner = select_winner(group)
    assert winner.winner.id == "b"
    assert not winner.coin_flip


def test_select_winner_falls_back_to_retrievals() -> None:
    members = (
        CollisionMember(
            id="a",
            type="memory",
            title="A",
            path="memories/a.md",
            importance=0.5,
            retrievals=10,
            updated=None,
        ),
        CollisionMember(
            id="b",
            type="memory",
            title="B",
            path="memories/b.md",
            importance=0.5,
            retrievals=2,
            updated=None,
        ),
    )
    from memstem.hygiene.cleanup_retro import CollisionGroup

    group = CollisionGroup(body_hash="h", members=members)
    winner = select_winner(group)
    assert winner.winner.id == "a"


def test_select_winner_coin_flip_when_signals_match() -> None:
    members = (
        CollisionMember(
            id="aaa",
            type="memory",
            title="A",
            path="memories/a.md",
            importance=0.5,
            retrievals=0,
            updated=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        CollisionMember(
            id="bbb",
            type="memory",
            title="B",
            path="memories/b.md",
            importance=0.5,
            retrievals=0,
            updated=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    from memstem.hygiene.cleanup_retro import CollisionGroup

    group = CollisionGroup(body_hash="h", members=members)
    winner = select_winner(group)
    assert winner.coin_flip
    # Lex tiebreak: "aaa" < "bbb"
    assert winner.winner.id == "aaa"


# ─── find_dedup_collisions ──────────────────────────────────────────


def test_find_dedup_collisions_groups_byte_identical(vault: Vault, index: Index) -> None:
    body = "shared content for the dedup test"
    a = _write_memory(vault, index, title="A", body=body)
    b = _write_memory(vault, index, title="B", body=body)
    c = _write_memory(vault, index, title="C", body="different content entirely")

    plan = find_dedup_collisions(vault, index)
    assert len(plan.groups) == 1
    group = plan.groups[0]
    member_ids = {m.id for m in group.members}
    assert member_ids == {str(a.id), str(b.id)}
    assert str(c.id) not in member_ids


def test_find_dedup_collisions_skips_already_deprecated(vault: Vault, index: Index) -> None:
    """Records already deprecated by an earlier pass are excluded."""
    body = "shared content"
    a = _write_memory(vault, index, title="A", body=body)
    b = _write_memory(vault, index, title="B", body=body)

    # Manually deprecate `b` in the index (simulating a prior pass).
    new_fm = b.frontmatter.model_copy(update={"deprecated_by": a.id})
    new_b = Memory(frontmatter=new_fm, body=b.body, path=b.path)
    vault.write(new_b)
    index.upsert(new_b)

    plan = find_dedup_collisions(vault, index)
    # Only `a` survives; group of size 1 is not a collision.
    assert plan.groups == ()


def test_find_dedup_collisions_flags_skill_groups(vault: Vault, index: Index) -> None:
    """Groups that contain a skill member are flagged for review queue routing."""
    body = "shared skill content for the test"
    _write_memory(vault, index, title="skill a", body=body, type_="skill")
    _write_memory(vault, index, title="skill b", body=body, type_="skill")

    plan = find_dedup_collisions(vault, index)
    assert len(plan.groups) == 1
    assert plan.groups[0].involves_skill


# ─── apply_dedup_collisions ─────────────────────────────────────────


def test_apply_dedup_writes_deprecated_by(vault: Vault, index: Index) -> None:
    body = "duplicated content"
    a = _write_memory(vault, index, title="A", body=body, importance=0.9)
    b = _write_memory(vault, index, title="B", body=body, importance=0.4)

    plan = find_dedup_collisions(vault, index)
    result = apply_dedup_collisions(vault, index, plan)
    assert result.deprecated == 1
    assert result.skill_review_tickets == 0
    assert result.audit_rows == 1

    # Higher-importance record (a) wins; b gets deprecated_by pointing
    # at a.
    a_disk = vault.read(a.path)
    b_disk = vault.read(b.path)
    assert a_disk.frontmatter.deprecated_by is None
    assert b_disk.frontmatter.deprecated_by == a.id


def test_apply_dedup_skips_skill_groups_writes_review_ticket(vault: Vault, index: Index) -> None:
    body = "shared skill content for review"
    _write_memory(vault, index, title="skill alpha", body=body, type_="skill")
    _write_memory(vault, index, title="skill beta", body=body, type_="skill")

    plan = find_dedup_collisions(vault, index)
    result = apply_dedup_collisions(vault, index, plan)
    assert result.deprecated == 0
    assert result.skill_review_tickets == 1
    # Both records remain non-deprecated.
    walked = list(vault.walk(types=["skill"]))
    assert all(m.frontmatter.deprecated_by is None for m in walked)
    # Review ticket exists.
    review_dir = vault.root / SKILL_REVIEW_DIRNAME
    assert review_dir.is_dir()
    tickets = list(review_dir.glob("*.md"))
    assert len(tickets) == 1
    ticket_text = tickets[0].read_text()
    assert "Skill collision review ticket" in ticket_text
    assert "winner" in ticket_text.lower()


def test_apply_dedup_idempotent(vault: Vault, index: Index) -> None:
    """Re-running cleanup-retro on a clean vault produces no changes."""
    body = "duplicated content X"
    _write_memory(vault, index, title="A", body=body)
    _write_memory(vault, index, title="B", body=body)

    plan = find_dedup_collisions(vault, index)
    apply_dedup_collisions(vault, index, plan)

    # Second pass: no collision groups now.
    plan2 = find_dedup_collisions(vault, index)
    assert plan2.groups == ()


def test_write_skill_review_ticket_format(vault: Vault, index: Index) -> None:
    body = "shared content"
    _write_memory(vault, index, title="skill q", body=body, type_="skill")
    _write_memory(vault, index, title="skill r", body=body, type_="skill")
    plan = find_dedup_collisions(vault, index)
    assert len(plan.groups) == 1
    ticket_path = write_skill_review_ticket(vault, plan.groups[0], plan.winners[0])
    assert ticket_path.parts[0] == "skills"
    assert ticket_path.parts[1] == "_review"
    full = vault.root / ticket_path
    text = full.read_text()
    assert "Skill collision review ticket" in text
    assert plan.winners[0].winner.id in text


# ─── find_noise_hits / apply_noise_expiry ──────────────────────────


def test_find_noise_hits_drops_heartbeat_records(vault: Vault, index: Index) -> None:
    """A record whose body matches a noise pattern is flagged for drop."""
    # Heartbeat: matches `^\s*HEARTBEAT_OK\s*$` line in body.
    _write_memory(vault, index, title="HB", body="HEARTBEAT_OK\n")
    _write_memory(
        vault,
        index,
        title="Real content",
        body="The legitimate body of a record that has nothing to do with cron.",
    )
    plan = find_noise_hits(vault, index)
    assert len(plan.drops) == 1
    assert plan.drops[0].decision.kind == "heartbeat"


def test_apply_noise_expiry_sets_valid_to(vault: Vault, index: Index) -> None:
    mem = _write_memory(vault, index, title="HB", body="HEARTBEAT_OK\n")
    plan = find_noise_hits(vault, index)
    result = apply_noise_expiry(vault, index, plan)
    assert result.expired == 1
    on_disk = vault.read(mem.path)
    assert on_disk.frontmatter.valid_to is not None


def test_format_dedup_report_includes_summary(vault: Vault, index: Index) -> None:
    body = "shared body"
    _write_memory(vault, index, title="A", body=body)
    _write_memory(vault, index, title="B", body=body)
    plan = find_dedup_collisions(vault, index)
    text = format_dedup_report(plan)
    assert "RETRO DEDUP PLAN" in text
    assert "Collision groups:" in text
    assert "Records to deprecate:" in text


def test_format_noise_report_includes_summary(vault: Vault, index: Index) -> None:
    _write_memory(vault, index, title="HB", body="HEARTBEAT_OK\n")
    plan = find_noise_hits(vault, index)
    text = format_noise_report(plan)
    assert "RETRO NOISE PLAN" in text
    assert "drop" in text.lower()


# ─── Search filter integration ──────────────────────────────────────


def test_search_excludes_deprecated_records_by_default(vault: Vault, index: Index) -> None:
    """A record with deprecated_by set should not surface in default search."""
    a = _write_memory(vault, index, title="winner", body="apple banana cherry")
    b = _write_memory(vault, index, title="loser", body="apple banana cherry")

    # Sanity: both records surface before deprecation.
    search = Search(vault=vault, index=index, embedder=None)
    pre = search.search("apple banana", limit=10, log_client=None)
    pre_ids = {str(r.memory.id) for r in pre}
    assert str(a.id) in pre_ids
    assert str(b.id) in pre_ids

    # Run cleanup-retro.
    plan = find_dedup_collisions(vault, index)
    apply_dedup_collisions(vault, index, plan)

    post = search.search("apple banana", limit=10, log_client=None)
    post_ids = {str(r.memory.id) for r in post}
    # One survives; the deprecated one is filtered.
    assert len(post_ids) == 1
    assert post_ids.issubset({str(a.id), str(b.id)})


def test_search_include_deprecated_returns_them(vault: Vault, index: Index) -> None:
    body = "duplicate body for include-deprecated"
    a = _write_memory(vault, index, title="winner", body=body)
    _write_memory(vault, index, title="loser", body=body)
    plan = find_dedup_collisions(vault, index)
    apply_dedup_collisions(vault, index, plan)

    search = Search(vault=vault, index=index, embedder=None)
    inclusive = search.search(
        "duplicate body include",
        limit=10,
        include_deprecated=True,
        log_client=None,
    )
    assert len(inclusive) == 2
    # Winner ID is still in the set.
    assert str(a.id) in {str(r.memory.id) for r in inclusive}
