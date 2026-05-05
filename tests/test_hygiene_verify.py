"""Tests for ``memstem.hygiene.verify``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.hygiene.verify import (
    VerifyReport,
    format_report,
    verify_vault,
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
    type_: str = "memory",
    title: str | None = None,
    body: str = "hello world",
    importance: float | None = None,
    deprecated_by: str | None = None,
    valid_to: str | None = None,
    extra_meta: dict[str, object] | None = None,
) -> Memory:
    """Helper that writes one memory through vault + index."""
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "test",
        "title": title or f"item-{type_}",
    }
    if importance is not None:
        metadata["importance"] = importance
    if deprecated_by is not None:
        metadata["deprecated_by"] = deprecated_by
    if valid_to is not None:
        metadata["valid_to"] = valid_to
    if type_ == "skill":
        metadata["scope"] = "universal"
        metadata["verification"] = "verify by hand"
    if extra_meta:
        metadata.update(extra_meta)
    fm = validate(metadata)
    folder = {
        "memory": "memories",
        "skill": "skills",
        "session": "sessions",
        "daily": "daily",
        "distillation": "distillations",
        "project": "projects",
    }[type_]
    path = Path(f"{folder}/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    index.upsert(memory)
    return memory


def test_empty_vault_reports_zeros(vault: Vault, index: Index) -> None:
    report = verify_vault(vault, index)
    assert isinstance(report, VerifyReport)
    assert report.total_memories == 0
    assert report.deprecated_total == 0
    assert report.valid_to_total == 0
    assert report.distilled_session_targets == 0
    assert report.undistilled_eligible_sessions == 0
    assert report.active_dedup_groups == 0
    assert report.skill_review_tickets == 0
    assert report.parser_skips == []


def test_type_breakdown_counts_per_type(vault: Vault, index: Index) -> None:
    _write_memory(vault, index, type_="memory")
    _write_memory(vault, index, type_="memory")
    _write_memory(vault, index, type_="skill", title="One Skill")
    _write_memory(vault, index, type_="session", body="lots of words " * 200)
    report = verify_vault(vault, index)

    counts = {t.type: t.total for t in report.by_type}
    assert counts == {"memory": 2, "skill": 1, "session": 1}
    assert report.total_memories == 4


def test_deprecated_and_valid_to_counted(vault: Vault, index: Index) -> None:
    winner = _write_memory(vault, index, type_="memory", title="winner")
    _write_memory(
        vault,
        index,
        type_="memory",
        title="loser",
        deprecated_by=str(winner.id),
    )
    _write_memory(
        vault,
        index,
        type_="memory",
        title="ttl",
        valid_to="2099-01-01T00:00:00+00:00",
    )
    report = verify_vault(vault, index)
    assert report.deprecated_total == 1
    assert report.valid_to_total == 1


def test_active_dedup_groups_detected(vault: Vault, index: Index) -> None:
    """Two records with identical bodies show up as a collision group."""
    _write_memory(vault, index, type_="memory", title="a", body="same body")
    _write_memory(vault, index, type_="memory", title="b", body="same body")
    report = verify_vault(vault, index)
    assert report.active_dedup_groups == 1
    assert report.active_dedup_to_deprecate == 1
    assert report.active_dedup_skill_groups == 0


def test_skill_collision_routed_to_skill_groups(vault: Vault, index: Index) -> None:
    """A skill-involved collision should land in the skill-review bucket,
    not in the auto-deprecate bucket."""
    _write_memory(vault, index, type_="skill", title="dup-skill", body="same skill body")
    _write_memory(vault, index, type_="memory", title="dup-mem", body="same skill body")
    report = verify_vault(vault, index)
    assert report.active_dedup_groups == 1
    assert report.active_dedup_skill_groups == 1


def test_skill_review_ticket_count(vault: Vault, index: Index) -> None:
    """Files under ``skills/_review/`` are counted as open tickets even
    though they're not memory documents and don't appear in any walk."""
    review_dir = vault.root / "skills" / "_review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "ticket-a.md").write_text("ticket a", encoding="utf-8")
    (review_dir / "ticket-b.md").write_text("ticket b", encoding="utf-8")
    report = verify_vault(vault, index)
    assert report.skill_review_tickets == 2
    # Also verify the review dir didn't introduce parser skips — Vault.walk
    # must skip underscore-prefixed dirs silently.
    assert report.parser_skips == []


def _meaningful_session_body() -> str:
    """Body that clears the default 10-turn / 100-word session threshold.

    ``is_meaningful_session`` counts ``**User:**`` / ``**Assistant:**``
    line prefixes when no explicit ``turn_count`` metadata is present.
    """
    turns: list[str] = []
    for i in range(15):
        turns.append(f"**User:** prompt number {i} with several real words")
        turns.append(f"**Assistant:** answer number {i} with even more real words here")
    return "\n\n".join(turns)


def test_undistilled_eligible_session_count(vault: Vault, index: Index) -> None:
    """A meaningful session with no distillation companion should be
    counted as an outstanding backfill target."""
    _write_memory(
        vault,
        index,
        type_="session",
        title="long session",
        body=_meaningful_session_body(),
    )
    report = verify_vault(vault, index)
    assert report.undistilled_eligible_sessions == 1
    assert report.distilled_session_targets == 0


def test_distilled_session_subtracts_from_eligible(vault: Vault, index: Index) -> None:
    """If a distillation links back to a session, that session is no
    longer counted as undistilled-eligible."""
    session = _write_memory(
        vault,
        index,
        type_="session",
        title="long session",
        body=_meaningful_session_body(),
    )
    session_stem = Path(session.path).stem
    _write_memory(
        vault,
        index,
        type_="distillation",
        title="distillation of long session",
        body="rollup",
        extra_meta={"links": [f"sessions/{session_stem}.md"]},
    )
    report = verify_vault(vault, index)
    assert report.distilled_session_targets == 1
    assert report.undistilled_eligible_sessions == 0


def test_format_report_renders_all_sections(vault: Vault, index: Index) -> None:
    _write_memory(vault, index, type_="memory")
    text = format_report(verify_vault(vault, index))
    assert "MEMSTEM VERIFY" in text
    assert "By type:" in text
    assert "Cleanup state:" in text
    assert "Derived records:" in text
    assert "Parser/validation skips during scan" in text


def test_as_json_is_serializable(vault: Vault, index: Index) -> None:
    import json

    _write_memory(vault, index, type_="memory")
    payload = verify_vault(vault, index).as_json()
    # Round-trips cleanly — no datetime objects, no Path objects, etc.
    text = json.dumps(payload)
    reloaded = json.loads(text)
    assert reloaded["total_memories"] == 1
    assert isinstance(reloaded["by_type"], list)


def test_parser_skips_counted_when_warnings_emit(vault: Vault, index: Index) -> None:
    """A file with malformed frontmatter under a non-reserved dir should
    surface as a parser_skips entry — that's how an operator notices
    schema breakage that ``hygiene cleanup-retro`` won't catch."""
    bad = vault.root / "memories" / "broken.md"
    bad.write_text("---\ntype: memory\n---\nbody\n", encoding="utf-8")
    report = verify_vault(vault, index)
    assert len(report.parser_skips) == 1
    assert "broken.md" in report.parser_skips[0]
