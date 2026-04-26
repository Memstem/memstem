"""Tests for the record → memory ingestion pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault


def _record(
    *,
    source: str = "openclaw",
    ref: str = "/tmp/test.md",
    title: str | None = "Test",
    body: str = "hello world",
    type_: str = "memory",
    tags: list[str] | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> MemoryRecord:
    metadata: dict[str, object] = {
        "type": type_,
        "created": "2026-04-25T10:00:00+00:00",
        "updated": "2026-04-25T10:00:00+00:00",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return MemoryRecord(
        source=source,
        ref=ref,
        title=title,
        body=body,
        tags=tags or [],
        metadata=metadata,
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


class TestPathForMemory:
    def test_skill_uses_title_slug(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(
            _record(
                title="Deploy via Cloudflare",
                type_="skill",
                extra_metadata={
                    "raw_frontmatter": {"scope": "universal", "verification": "ok"},
                },
            )
        )
        assert memory.path == Path("skills/deploy-via-cloudflare.md")

    def test_memory_lives_under_source_subdir(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(source="openclaw"))
        assert memory.path.parts[0] == "memories"
        assert memory.path.parts[1] == "openclaw"

    def test_session_uses_session_id_metadata(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(
            _record(
                type_="session",
                extra_metadata={"session_id": "sess-1234"},
            )
        )
        assert memory.path == Path("sessions/sess-1234.md")

    def test_agent_tag_disambiguates_skill_path(self, vault: Vault, index: Index) -> None:
        """Two agents with the same skill title must not collide on disk."""
        pipe = Pipeline(vault, index)
        ari = pipe.process(
            _record(
                ref="/home/ubuntu/ari/skills/deploy/SKILL.md",
                title="Deploy",
                type_="skill",
                tags=["agent:ari"],
                extra_metadata={
                    "raw_frontmatter": {"scope": "universal", "verification": "ok"},
                },
            )
        )
        sarah = pipe.process(
            _record(
                ref="/home/ubuntu/sarah/skills/deploy/SKILL.md",
                title="Deploy",
                type_="skill",
                tags=["agent:sarah"],
                extra_metadata={
                    "raw_frontmatter": {"scope": "universal", "verification": "ok"},
                },
            )
        )
        assert ari.path == Path("skills/ari/deploy.md")
        assert sarah.path == Path("skills/sarah/deploy.md")
        assert ari.path != sarah.path

    def test_agent_tag_disambiguates_daily_path(self, vault: Vault, index: Index) -> None:
        """Daily logs from different agents on the same date must not collide."""
        pipe = Pipeline(vault, index)
        ari = pipe.process(
            _record(
                ref="/home/ubuntu/ari/memory/2026-04-26.md",
                title="2026-04-26",
                type_="daily",
                tags=["agent:ari"],
                extra_metadata={"created": "2026-04-26T00:00:00+00:00"},
            )
        )
        sarah = pipe.process(
            _record(
                ref="/home/ubuntu/sarah/memory/2026-04-26.md",
                title="2026-04-26",
                type_="daily",
                tags=["agent:sarah"],
                extra_metadata={"created": "2026-04-26T00:00:00+00:00"},
            )
        )
        assert ari.path == Path("daily/ari/2026-04-26.md")
        assert sarah.path == Path("daily/sarah/2026-04-26.md")

    def test_no_agent_tag_keeps_legacy_path(self, vault: Vault, index: Index) -> None:
        """Records without an `agent:` tag use the pre-PR-25 paths (back-compat)."""
        pipe = Pipeline(vault, index)
        skill = pipe.process(
            _record(
                title="Deploy",
                type_="skill",
                extra_metadata={
                    "raw_frontmatter": {"scope": "universal", "verification": "ok"},
                },
            )
        )
        daily = pipe.process(
            _record(
                ref="/tmp/2026-04-26.md",
                title="2026-04-26",
                type_="daily",
                extra_metadata={"created": "2026-04-26T00:00:00+00:00"},
            )
        )
        assert skill.path == Path("skills/deploy.md")
        assert daily.path == Path("daily/2026-04-26.md")


class TestProcess:
    def test_creates_memory_in_vault(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="hello"))
        on_disk = vault.read(memory.path)
        assert on_disk.body == "hello"
        # Index row exists.
        row = index.db.execute("SELECT id FROM memories WHERE id = ?", (str(memory.id),)).fetchone()
        assert row is not None

    def test_re_emit_updates_existing_memory(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        first = pipe.process(_record(body="v1"))
        second = pipe.process(_record(body="v2"))
        # Same source+ref → same id, same path.
        assert first.id == second.id
        assert first.path == second.path
        assert vault.read(second.path).body == "v2"

    def test_distinct_refs_get_distinct_ids(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        a = pipe.process(_record(ref="/a.md"))
        b = pipe.process(_record(ref="/b.md"))
        assert a.id != b.id

    def test_different_sources_get_distinct_ids_for_same_ref(
        self, vault: Vault, index: Index
    ) -> None:
        pipe = Pipeline(vault, index)
        a = pipe.process(_record(source="openclaw", ref="/x.md"))
        b = pipe.process(_record(source="claude-code", ref="/x.md"))
        assert a.id != b.id

    def test_provenance_recorded_in_frontmatter(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(source="openclaw", ref="/path.md"))
        prov = memory.frontmatter.provenance
        assert prov is not None
        assert prov.source == "openclaw"
        assert prov.ref == "/path.md"

    def test_skill_with_missing_required_fields_uses_defaults(
        self, vault: Vault, index: Index
    ) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(type_="skill", title="My Skill"))
        assert memory.frontmatter.scope == "universal"
        assert memory.frontmatter.verification

    def test_invalid_iso_timestamp_falls_back_to_now(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(
            _record(extra_metadata={"created": "not-a-date", "updated": "also not"})
        )
        # Just verify a valid datetime was produced.
        assert memory.frontmatter.created is not None
        assert memory.frontmatter.updated is not None


class TestEmbedQueueing:
    """Pipeline writes records synchronously and pushes them onto the
    embed queue. The actual embedding is the worker's job (covered in
    `test_embed_worker.py`); these tests just verify the handoff."""

    def test_process_enqueues_record(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="hello"))
        rows = index.db.execute(
            "SELECT memory_id, retry_count, failed FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["retry_count"] == 0
        assert rows[0]["failed"] == 0

    def test_no_vec_rows_written_synchronously(self, vault: Vault, index: Index) -> None:
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="some body"))
        rows = index.db.execute(
            "SELECT COUNT(*) AS c FROM memories_vec WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert rows["c"] == 0

    def test_re_emit_resets_queue_state(self, vault: Vault, index: Index) -> None:
        """A failing record that the user edits should get another shot."""
        pipe = Pipeline(vault, index)
        memory = pipe.process(_record(body="v1"))
        index.mark_embed_error(str(memory.id), "boom", max_retries=1)
        # After the simulated failure the record is `failed=1`.
        row = index.db.execute(
            "SELECT failed FROM embed_queue WHERE memory_id = ?", (str(memory.id),)
        ).fetchone()
        assert row["failed"] == 1
        # Re-processing the same source+ref re-enqueues with cleared state.
        pipe.process(_record(body="v2"))
        row = index.db.execute(
            "SELECT failed, retry_count FROM embed_queue WHERE memory_id = ?",
            (str(memory.id),),
        ).fetchone()
        assert row["failed"] == 0
        assert row["retry_count"] == 0
