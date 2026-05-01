"""Tests for the project-records writer (ADR 0021).

Cover: project tag extraction, candidate discovery + threshold
filtering, distillation-vs-raw input preference, prompt construction
with chronological ordering, materialization shape (path, links,
title from H1), manual:true preservation, --force override, plan +
apply round trip, and the CLI subcommand.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import MemoryType, validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.core.summarizer import NoOpSummarizer, StubSummarizer
from memstem.hygiene.project_records import (
    DEFAULT_PROJECT_IMPORTANCE,
    PROJECT_KIND_TAG,
    PROVENANCE_REF_PREFIX,
    ProjectCandidate,
    apply_project_records,
    build_project_prompt,
    compute_project_record_plan,
    existing_project_record,
    find_project_candidates,
    is_manual,
    materialize_project_record,
    project_tag_for_session,
)

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in (
        "memories",
        "memories/projects",
        "skills",
        "sessions",
        "daily",
        "distillations",
        "_meta",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Generator[Index, None, None]:
    db_path = tmp_path / "vault" / "_meta" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    idx = Index(db_path)
    idx.connect()
    yield idx
    idx.close()


def _write_session(
    vault: Vault,
    *,
    session_id: str,
    title: str = "test session",
    body: str = "**User:** stuff\n\n**Assistant:** more stuff",
    tags: list[str] | None = None,
    source: str = "claude-code",
    updated: datetime | None = None,
) -> Memory:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "session",
        "created": (updated or datetime(2026, 4, 21, tzinfo=UTC)).isoformat(),
        "updated": (updated or datetime(2026, 4, 28, tzinfo=UTC)).isoformat(),
        "source": source,
        "title": title,
        "tags": tags or [],
    }
    fm = validate(payload)
    memory = Memory(
        frontmatter=fm,
        body=body,
        path=Path(f"sessions/{session_id}.md"),
    )
    vault.write(memory)
    return memory


def _write_distillation(
    vault: Vault,
    *,
    linked_session_id: str,
    body: str = "## Summary\n\nbody about the session",
    title: str = "Distillation",
    tags: list[str] | None = None,
) -> Memory:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "distillation",
        "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "source": "hygiene-worker",
        "title": title,
        "tags": tags or ["distillation:session"],
        "links": [f"memory://sessions/{linked_session_id}"],
        "importance": 0.8,
    }
    fm = validate(payload)
    memory = Memory(
        frontmatter=fm,
        body=body,
        path=Path(f"distillations/claude-code/{linked_session_id}.md"),
    )
    vault.write(memory)
    return memory


# ─── project_tag_for_session ──────────────────────────────────────


class TestProjectTagForSession:
    def test_returns_first_project_tag(self, vault: Vault) -> None:
        m = _write_session(
            vault,
            session_id="s1",
            tags=["home-ubuntu-woodfield-quotes"],
        )
        assert project_tag_for_session(m) == "home-ubuntu-woodfield-quotes"

    def test_skips_agent_prefix(self, vault: Vault) -> None:
        m = _write_session(vault, session_id="s2", tags=["agent:ari"])
        assert project_tag_for_session(m) is None

    def test_skips_topic_prefix(self, vault: Vault) -> None:
        m = _write_session(vault, session_id="s3", tags=["topic:cloudflare"])
        assert project_tag_for_session(m) is None

    def test_skips_distillation_prefix(self, vault: Vault) -> None:
        m = _write_session(vault, session_id="s4", tags=["distillation:session"])
        assert project_tag_for_session(m) is None

    def test_skips_literal_reserved_tags(self, vault: Vault) -> None:
        m = _write_session(vault, session_id="s5", tags=["instructions"])
        assert project_tag_for_session(m) is None

    def test_finds_first_when_mixed(self, vault: Vault) -> None:
        m = _write_session(
            vault,
            session_id="s6",
            tags=["agent:ari", "home-ubuntu-stuff"],
        )
        assert project_tag_for_session(m) == "home-ubuntu-stuff"

    def test_non_session_returns_none(self, vault: Vault) -> None:
        payload = {
            "id": str(uuid4()),
            "type": "memory",
            "created": datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "test",
            "title": "x",
            "tags": ["home-ubuntu-something"],
        }
        m = Memory(
            frontmatter=validate(payload),
            body="x",
            path=Path("memories/test.md"),
        )
        assert project_tag_for_session(m) is None


# ─── find_project_candidates ──────────────────────────────────────


class TestFindProjectCandidates:
    def test_empty_vault(self, vault: Vault) -> None:
        candidates, stats = find_project_candidates(vault)
        assert candidates == []
        assert stats["total_tags_scanned"] == 0

    def test_single_session_below_threshold(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", tags=["home-ubuntu-onesession"])
        candidates, stats = find_project_candidates(vault, min_sessions=2)
        assert candidates == []
        assert stats["skipped_below_threshold"] == 1

    def test_two_sessions_create_a_candidate(self, vault: Vault) -> None:
        _write_session(
            vault,
            session_id="s1",
            tags=["home-ubuntu-woodfield-quotes"],
            title="proposal review",
        )
        _write_session(
            vault,
            session_id="s2",
            tags=["home-ubuntu-woodfield-quotes"],
            title="video work",
            updated=datetime(2026, 4, 29, tzinfo=UTC),
        )
        candidates, _ = find_project_candidates(vault)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.slug == "home-ubuntu-woodfield-quotes"
        assert c.session_count == 2
        # Sorted chronologically by `updated`.
        ids = [Path(s.path).stem for s in c.sessions]
        assert ids == ["s1", "s2"]

    def test_distillations_attached_to_candidate(self, vault: Vault) -> None:
        _write_session(
            vault,
            session_id="s1",
            tags=["home-ubuntu-woodfield-quotes"],
        )
        _write_session(
            vault,
            session_id="s2",
            tags=["home-ubuntu-woodfield-quotes"],
        )
        _write_distillation(vault, linked_session_id="s1", body="d1")
        candidates, _ = find_project_candidates(vault)
        c = candidates[0]
        assert len(c.distillations) == 1
        # The one distillation belongs to s1.
        d = c.distillations[0]
        assert "memory://sessions/s1" in d.frontmatter.links

    def test_multiple_projects_each_qualify(self, vault: Vault) -> None:
        _write_session(vault, session_id="a1", tags=["proj-a"])
        _write_session(vault, session_id="a2", tags=["proj-a"])
        _write_session(vault, session_id="b1", tags=["proj-b"])
        _write_session(vault, session_id="b2", tags=["proj-b"])
        candidates, _ = find_project_candidates(vault)
        slugs = sorted(c.slug for c in candidates)
        assert slugs == ["proj-a", "proj-b"]


# ─── build_project_prompt ─────────────────────────────────────────


def _make_candidate(
    *,
    slug: str = "home-ubuntu-woodfield-quotes",
    sessions: list[Memory] | None = None,
    distillations: list[Memory] | None = None,
) -> ProjectCandidate:
    sessions = sessions or []
    distillations = distillations or []
    return ProjectCandidate(
        slug=slug,
        sessions=sessions,
        distillations=distillations,
        earliest_created=datetime(2026, 4, 21, tzinfo=UTC),
        latest_updated=datetime(2026, 4, 29, tzinfo=UTC),
    )


class TestBuildProjectPrompt:
    def test_includes_slug_and_session_count(self, vault: Vault) -> None:
        s1 = _write_session(vault, session_id="s1", tags=["home-ubuntu-woodfield-quotes"])
        s2 = _write_session(vault, session_id="s2", tags=["home-ubuntu-woodfield-quotes"])
        candidate = _make_candidate(sessions=[s1, s2])
        prompt = build_project_prompt(candidate)
        assert "home-ubuntu-woodfield-quotes" in prompt
        assert "NUMBER OF SOURCE SESSIONS: 2" in prompt

    def test_uses_distillation_when_available(self, vault: Vault) -> None:
        s1 = _write_session(
            vault, session_id="s1", tags=["home-ubuntu-x"], body="raw session content"
        )
        d1 = _write_distillation(
            vault,
            linked_session_id="s1",
            body="distillation body for s1",
        )
        candidate = _make_candidate(sessions=[s1], distillations=[d1])
        prompt = build_project_prompt(candidate)
        assert "distillation body for s1" in prompt
        assert "raw session content" not in prompt
        assert "[distillation]" in prompt
        assert "input_type" not in prompt  # the literal placeholder shouldn't leak

    def test_falls_back_to_session_body_when_no_distillation(self, vault: Vault) -> None:
        s1 = _write_session(
            vault, session_id="s1", tags=["home-ubuntu-x"], body="raw session content"
        )
        candidate = _make_candidate(sessions=[s1], distillations=[])
        prompt = build_project_prompt(candidate)
        assert "raw session content" in prompt
        assert "[session]" in prompt

    def test_mixed_input_type_label(self, vault: Vault) -> None:
        s1 = _write_session(vault, session_id="s1", tags=["home-ubuntu-x"], body="s1 body")
        s2 = _write_session(vault, session_id="s2", tags=["home-ubuntu-x"], body="s2 body")
        d1 = _write_distillation(vault, linked_session_id="s1", body="d1 body")
        candidate = _make_candidate(sessions=[s1, s2], distillations=[d1])
        prompt = build_project_prompt(candidate)
        assert "mixed (1 distillation, 1 raw session)" in prompt

    def test_truncates_oversize_inputs(self, vault: Vault) -> None:
        big = "x" * 5000
        s1 = _write_session(vault, session_id="s1", tags=["t"], body=big)
        s2 = _write_session(vault, session_id="s2", tags=["t"], body=big)
        s3 = _write_session(vault, session_id="s3", tags=["t"], body=big)
        candidate = _make_candidate(sessions=[s1, s2, s3])
        prompt = build_project_prompt(candidate, max_input_chars=8000)
        assert "[…input continues for" in prompt


# ─── materialize_project_record ───────────────────────────────────


class TestMaterializeProjectRecord:
    def test_path_uses_slug(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["home-ubuntu-woodfield-quotes"])
        candidate = _make_candidate(sessions=[s])
        body = "# Woodfield Country Club — e-bike work\n\n## Description\n\nbody"
        memory = materialize_project_record(candidate, body, "stub")
        assert memory.path == Path("memories/projects/home-ubuntu-woodfield-quotes.md")

    def test_title_extracted_from_body_h1(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["t"])
        candidate = _make_candidate(slug="t", sessions=[s])
        body = "# Custom Project Title\n\n## Description\n\nstuff"
        memory = materialize_project_record(candidate, body, "stub")
        assert memory.frontmatter.title == "Custom Project Title"

    def test_title_falls_back_to_slug_when_no_h1(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["my-slug"])
        candidate = _make_candidate(slug="my-slug", sessions=[s])
        body = "## Description\n\nno H1 line at the top"
        memory = materialize_project_record(candidate, body, "stub")
        assert memory.frontmatter.title == "my-slug"

    def test_links_include_sessions_and_distillations(self, vault: Vault) -> None:
        s1 = _write_session(vault, session_id="s1", tags=["t"])
        s2 = _write_session(vault, session_id="s2", tags=["t"])
        d1 = _write_distillation(vault, linked_session_id="s1")
        candidate = _make_candidate(sessions=[s1, s2], distillations=[d1])
        memory = materialize_project_record(candidate, "# Title\n\nbody", "stub")
        links = set(memory.frontmatter.links)
        assert "memory://sessions/s1" in links
        assert "memory://sessions/s2" in links
        # Distillation link uses the distillation's own id (not the session id)
        assert any("distillations" in link for link in memory.frontmatter.links)

    def test_default_importance_is_seed(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["t"])
        candidate = _make_candidate(sessions=[s])
        memory = materialize_project_record(candidate, "# T\n\nbody", "stub")
        assert memory.frontmatter.importance == DEFAULT_PROJECT_IMPORTANCE

    def test_provenance_ref_uses_slug(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["my-slug"])
        candidate = _make_candidate(slug="my-slug", sessions=[s])
        memory = materialize_project_record(candidate, "# T\n\nbody", "stub")
        prov = memory.frontmatter.provenance
        assert prov is not None
        assert prov.ref == f"{PROVENANCE_REF_PREFIX}my-slug"

    def test_tags_include_slug_and_kind_marker(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["my-slug"])
        candidate = _make_candidate(slug="my-slug", sessions=[s])
        memory = materialize_project_record(candidate, "# T\n\nbody", "stub")
        tags = set(memory.frontmatter.tags)
        assert "my-slug" in tags
        assert PROJECT_KIND_TAG in tags

    def test_existing_record_preserves_id_and_created(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["t"])
        candidate = _make_candidate(sessions=[s])
        # First run.
        first = materialize_project_record(candidate, "# T\n\noriginal", "stub")
        # Simulate "existing" by using the first as the existing handle.
        second = materialize_project_record(candidate, "# T\n\nupdated", "stub", existing=first)
        assert second.id == first.id
        assert second.frontmatter.created == first.frontmatter.created
        assert second.body == "# T\n\nupdated"

    def test_preserve_manual_body_keeps_existing_body(self, vault: Vault) -> None:
        s = _write_session(vault, session_id="s1", tags=["t"])
        candidate = _make_candidate(sessions=[s])
        first = materialize_project_record(candidate, "# Hand Edited\n\nuser content", "stub")
        # Apply a "fresh" LLM body but with preserve_manual_body=True.
        second = materialize_project_record(
            candidate,
            "# AI Title\n\nLLM-generated body",
            "stub",
            existing=first,
            preserve_manual_body=True,
        )
        assert second.body == "# Hand Edited\n\nuser content"
        # Manual flag is set so future writers can short-circuit.
        extra = getattr(second.frontmatter, "model_extra", None) or {}
        assert extra.get("manual") is True


# ─── is_manual ────────────────────────────────────────────────────


def test_is_manual_detects_true(vault: Vault) -> None:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "project",
        "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "source": "human",
        "title": "x",
        "manual": True,
    }
    m = Memory(
        frontmatter=validate(payload),
        body="x",
        path=Path("memories/projects/x.md"),
    )
    assert is_manual(m) is True


def test_is_manual_default_false(vault: Vault) -> None:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "project",
        "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "source": "human",
        "title": "x",
    }
    m = Memory(
        frontmatter=validate(payload),
        body="x",
        path=Path("memories/projects/x.md"),
    )
    assert is_manual(m) is False


# ─── Plan + apply ─────────────────────────────────────────────────


class TestComputePlan:
    def test_empty_vault(self, vault: Vault) -> None:
        plan = compute_project_record_plan(vault, NoOpSummarizer())
        assert plan.proposals == []

    def test_noop_marks_proposals_as_empty(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", tags=["t"])
        _write_session(vault, session_id="s2", tags=["t"])
        plan = compute_project_record_plan(vault, NoOpSummarizer())
        assert len(plan.proposals) == 1
        prop = plan.proposals[0]
        assert prop.body == ""
        assert prop.skipped_reason is not None
        assert prop.is_update is False

    def test_stub_produces_body(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", tags=["proj-a"])
        _write_session(vault, session_id="s2", tags=["proj-a"])
        stub = StubSummarizer()
        stub.set_default("# Project A\n\n## Description\n\nbody")
        plan = compute_project_record_plan(vault, stub)
        assert len(plan.proposals) == 1
        assert plan.proposals[0].body == "# Project A\n\n## Description\n\nbody"

    def test_existing_record_marks_proposal_as_update(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", tags=["proj-a"])
        _write_session(vault, session_id="s2", tags=["proj-a"])
        # Pre-existing project record.
        payload: dict[str, object] = {
            "id": str(uuid4()),
            "type": "project",
            "created": datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "hygiene-worker",
            "title": "proj-a",
            "tags": ["proj-a", PROJECT_KIND_TAG],
            "links": [],
            "importance": DEFAULT_PROJECT_IMPORTANCE,
        }
        existing = Memory(
            frontmatter=validate(payload),
            body="old body",
            path=Path("memories/projects/proj-a.md"),
        )
        vault.write(existing)

        stub = StubSummarizer()
        stub.set_default("# proj-a\n\nnew body")
        plan = compute_project_record_plan(vault, stub)
        assert len(plan.proposals) == 1
        prop = plan.proposals[0]
        assert prop.is_update is True
        assert prop.existing_memory_id == str(existing.id)


class TestManualOverride:
    def _make_existing_manual(self, vault: Vault, slug: str = "proj-a") -> Memory:
        _write_session(vault, session_id="s1", tags=[slug])
        _write_session(vault, session_id="s2", tags=[slug])
        payload: dict[str, object] = {
            "id": str(uuid4()),
            "type": "project",
            "created": datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "human",
            "title": slug,
            "tags": [slug, PROJECT_KIND_TAG],
            "links": [],
            "importance": DEFAULT_PROJECT_IMPORTANCE,
            "manual": True,
        }
        existing = Memory(
            frontmatter=validate(payload),
            body="hand-edited body",
            path=Path(f"memories/projects/{slug}.md"),
        )
        vault.write(existing)
        return existing

    def test_manual_skip_without_force(self, vault: Vault) -> None:
        self._make_existing_manual(vault)
        stub = StubSummarizer()
        stub.set_default("# T\n\nLLM body")
        plan = compute_project_record_plan(vault, stub)
        prop = plan.proposals[0]
        assert prop.manual_skip is True
        # Body in proposal is the existing body (not the LLM output).
        assert prop.body == "hand-edited body"

    def test_force_overrides_manual(self, vault: Vault) -> None:
        self._make_existing_manual(vault)
        stub = StubSummarizer()
        stub.set_default("# T\n\nLLM body")
        plan = compute_project_record_plan(vault, stub, force=True)
        prop = plan.proposals[0]
        assert prop.manual_skip is False
        assert prop.body == "# T\n\nLLM body"


class TestApply:
    def test_apply_writes_new_record_and_indexes_it(self, vault: Vault, index: Index) -> None:
        _write_session(vault, session_id="s1", tags=["proj-a"])
        _write_session(vault, session_id="s2", tags=["proj-a"])
        stub = StubSummarizer()
        stub.set_default("# Project A\n\n## Description\n\nbody")
        plan = compute_project_record_plan(vault, stub)
        result = apply_project_records(vault, index, plan)
        assert result.written == 1
        assert result.updated == 0
        assert result.apply_errors == []
        # File exists at the canonical path.
        record = existing_project_record(vault, "proj-a")
        assert record is not None
        assert record.frontmatter.type is MemoryType.PROJECT
        # Index has it.
        row = index.db.execute(
            "SELECT id, type FROM memories WHERE id = ?", (str(record.id),)
        ).fetchone()
        assert row is not None
        assert row["type"] == MemoryType.PROJECT.value
        # And confirm the project record was enqueued for embedding so
        # vec retrieval can rank it above raw transcripts (BM25 alone
        # favors longer documents on term frequency).
        queue_row = index.db.execute(
            "SELECT memory_id FROM embed_queue WHERE memory_id = ?", (str(record.id),)
        ).fetchone()
        assert queue_row is not None

    def test_apply_refreshes_links_only_for_manual_record(self, vault: Vault, index: Index) -> None:
        # Set up a manual existing record.
        existing_payload: dict[str, object] = {
            "id": str(uuid4()),
            "type": "project",
            "created": datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "human",
            "title": "manual-proj",
            "tags": ["manual-proj", PROJECT_KIND_TAG],
            "links": [],
            "importance": DEFAULT_PROJECT_IMPORTANCE,
            "manual": True,
        }
        existing = Memory(
            frontmatter=validate(existing_payload),
            body="hand-curated body",
            path=Path("memories/projects/manual-proj.md"),
        )
        vault.write(existing)
        original_id = existing.id
        # Two sessions land for this project.
        _write_session(vault, session_id="s1", tags=["manual-proj"])
        _write_session(vault, session_id="s2", tags=["manual-proj"])
        # Run plan with a stub that *would* produce a body — but the
        # manual flag should bypass it.
        stub = StubSummarizer()
        stub.set_default("# Auto title\n\nAI body")
        plan = compute_project_record_plan(vault, stub)
        result = apply_project_records(vault, index, plan)
        assert result.links_only_updates == 1
        assert result.written == 0
        # Body preserved, links refreshed.
        refreshed = existing_project_record(vault, "manual-proj")
        assert refreshed is not None
        assert refreshed.id == original_id
        assert refreshed.body == "hand-curated body"
        # Links now include the two sessions.
        link_set = set(refreshed.frontmatter.links)
        assert "memory://sessions/s1" in link_set
        assert "memory://sessions/s2" in link_set

    def test_force_overwrite_replaces_manual_body(self, vault: Vault, index: Index) -> None:
        existing_payload: dict[str, object] = {
            "id": str(uuid4()),
            "type": "project",
            "created": datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "human",
            "title": "manual-proj",
            "tags": ["manual-proj", PROJECT_KIND_TAG],
            "links": [],
            "importance": DEFAULT_PROJECT_IMPORTANCE,
            "manual": True,
        }
        existing = Memory(
            frontmatter=validate(existing_payload),
            body="hand-curated body",
            path=Path("memories/projects/manual-proj.md"),
        )
        vault.write(existing)
        _write_session(vault, session_id="s1", tags=["manual-proj"])
        _write_session(vault, session_id="s2", tags=["manual-proj"])
        stub = StubSummarizer()
        stub.set_default("# Auto title\n\nAI body")
        plan = compute_project_record_plan(vault, stub, force=True)
        apply_project_records(vault, index, plan)
        refreshed = existing_project_record(vault, "manual-proj")
        assert refreshed is not None
        assert refreshed.body == "# Auto title\n\nAI body"


# ─── CLI ──────────────────────────────────────────────────────────


class TestCli:
    def _setup_vault(self, tmp_path: Path) -> Path:
        vault_path = tmp_path / "vault"
        for sub in (
            "memories",
            "memories/projects",
            "skills",
            "sessions",
            "daily",
            "distillations",
            "_meta",
        ):
            (vault_path / sub).mkdir(parents=True, exist_ok=True)
        v = Vault(vault_path)
        _write_session(v, session_id="s1", tags=["my-test-project"])
        _write_session(v, session_id="s2", tags=["my-test-project"])
        import yaml as _yaml

        cfg = {
            "vault_path": str(vault_path),
            "embedding": {"provider": "ollama"},
        }
        (vault_path / "_meta" / "config.yaml").write_text(_yaml.safe_dump(cfg), encoding="utf-8")
        return vault_path

    def test_cli_dry_run_with_noop(self, tmp_path: Path) -> None:
        vault_path = self._setup_vault(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "hygiene",
                "project-records",
                "--vault",
                str(vault_path),
                "--provider",
                "noop",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "1 project tag(s)" in result.output
        assert "my-test-project" in result.output
        assert "dry-run" in result.output

    def test_cli_unknown_provider_errors(self, tmp_path: Path) -> None:
        vault_path = self._setup_vault(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "hygiene",
                "project-records",
                "--vault",
                str(vault_path),
                "--provider",
                "voodoo",
            ],
        )
        assert result.exit_code == 2
        assert "unknown provider" in result.output
