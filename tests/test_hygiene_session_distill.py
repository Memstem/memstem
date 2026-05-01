"""Tests for the session-distillation writer (ADR 0020).

Cover: candidate discovery + threshold filtering, "already distilled"
skip behavior, prompt construction, materialization shape, the
plan/apply round trip with a stub summarizer, ``--force`` re-run
behavior, and the CLI subcommand.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import MemoryType, validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.core.summarizer import NoOpSummarizer, StubSummarizer
from memstem.hygiene.session_distill import (
    DEFAULT_DISTILLATION_IMPORTANCE,
    DISTILLATION_KIND_TAG,
    PROVENANCE_REF_PREFIX,
    SessionCandidate,
    apply_distillations,
    build_session_prompt,
    compute_distillation_plan,
    find_distilled_session_ids,
    find_session_candidates,
    is_meaningful_session,
    materialize_distillation,
)

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "distillations", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Generator[Index, None, None]:
    """A connected, isolated Index for apply tests."""
    db_path = tmp_path / "vault" / "_meta" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    idx = Index(db_path)
    idx.connect()
    yield idx
    idx.close()


def _session_body(turns: int, words_per_turn: int = 12) -> str:
    """Build a synthetic session body with N turns of substantive content."""
    lines: list[str] = []
    filler = " ".join(["word"] * words_per_turn)
    for i in range(turns):
        role = "User" if i % 2 == 0 else "Assistant"
        lines.append(f"**{role}:** {filler}")
        lines.append("")  # paragraph break
    return "\n".join(lines).strip()


def _write_session(
    vault: Vault,
    *,
    session_id: str | None = None,
    title: str = "test session",
    body: str | None = None,
    turns: int = 12,
    words_per_turn: int = 12,
    tags: list[str] | None = None,
    source: str = "claude-code",
    updated: datetime | None = None,
    turn_count: int | None = None,
) -> Memory:
    """Create a session record on disk and return its Memory."""
    sid = session_id or str(uuid4())
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "session",
        "created": (updated or datetime(2026, 4, 28, tzinfo=UTC)).isoformat(),
        "updated": (updated or datetime(2026, 4, 28, tzinfo=UTC)).isoformat(),
        "source": source,
        "title": title,
        "tags": tags or [],
    }
    if turn_count is not None:
        payload["turn_count"] = turn_count
    fm = validate(payload)
    body = body if body is not None else _session_body(turns, words_per_turn)
    memory = Memory(frontmatter=fm, body=body, path=Path(f"sessions/{sid}.md"))
    vault.write(memory)
    return memory


def _write_distillation(
    vault: Vault,
    *,
    linked_session_id: str,
    title: str = "Distillation — test session",
    body: str = "## Summary\n\nbody",
    source: str = "hygiene-worker",
    tags: list[str] | None = None,
    importance: float = DEFAULT_DISTILLATION_IMPORTANCE,
) -> Memory:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "distillation",
        "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "source": source,
        "title": title,
        "tags": tags or [DISTILLATION_KIND_TAG],
        "links": [f"memory://sessions/{linked_session_id}"],
        "importance": importance,
        "provenance": {
            "source": source,
            "ref": f"{PROVENANCE_REF_PREFIX}{linked_session_id}",
            "ingested_at": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        },
    }
    fm = validate(payload)
    return Memory(
        frontmatter=fm,
        body=body,
        path=Path(f"distillations/claude-code/{linked_session_id}.md"),
    )


# ─── Threshold + meaningfulness ───────────────────────────────────


class TestIsMeaningfulSession:
    def test_substantive_session_is_meaningful(self, vault: Vault) -> None:
        m = _write_session(vault, turns=12, words_per_turn=10)
        assert is_meaningful_session(m) is True

    def test_short_session_below_word_threshold(self, vault: Vault) -> None:
        m = _write_session(vault, turns=12, words_per_turn=2)  # ~24 words
        assert is_meaningful_session(m) is False

    def test_few_turns_below_threshold(self, vault: Vault) -> None:
        m = _write_session(vault, turns=4, words_per_turn=40)  # tons of words, few turns
        assert is_meaningful_session(m) is False

    def test_non_session_type_rejected(self, vault: Vault) -> None:
        # Build a memory record (not a session) to confirm the gate
        # early-returns False for the wrong type.
        payload = {
            "id": str(uuid4()),
            "type": "memory",
            "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
            "source": "test",
            "title": "x",
            "tags": [],
        }
        m = Memory(
            frontmatter=validate(payload),
            body=_session_body(20),
            path=Path("memories/test.md"),
        )
        assert is_meaningful_session(m) is False

    def test_threshold_uses_explicit_turn_count_when_present(self, vault: Vault) -> None:
        # 4 turn markers in body, but turn_count metadata says 20.
        # Custom min_turns = 10 → with explicit count, should pass.
        m = _write_session(vault, turns=4, words_per_turn=80, turn_count=20)
        assert is_meaningful_session(m, min_turns=10) is True

    def test_threshold_falls_back_to_body_marker_count(self, vault: Vault) -> None:
        # No turn_count metadata; rely on body marker count.
        m = _write_session(vault, turns=15, words_per_turn=10)
        assert is_meaningful_session(m, min_turns=12) is True
        # And confirm a smaller body fails.
        m2 = _write_session(vault, turns=8, words_per_turn=20)
        assert is_meaningful_session(m2, min_turns=12) is False


# ─── Candidate discovery ──────────────────────────────────────────


class TestFindSessionCandidates:
    def test_empty_vault_returns_empty(self, vault: Vault) -> None:
        candidates, stats = find_session_candidates(vault)
        assert candidates == []
        assert stats["total_sessions_scanned"] == 0

    def test_all_substantive_sessions_become_candidates(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12)
        _write_session(vault, session_id="s2", turns=14)
        candidates, stats = find_session_candidates(vault, recency_days=None)
        ids = sorted(c.session_id for c in candidates)
        assert ids == ["s1", "s2"]
        assert stats["total_sessions_scanned"] == 2

    def test_already_distilled_sessions_are_skipped(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12)
        _write_session(vault, session_id="s2", turns=12)
        # Distill s1 already.
        d = _write_distillation(vault, linked_session_id="s1")
        vault.write(d)
        candidates, stats = find_session_candidates(vault, recency_days=None)
        ids = sorted(c.session_id for c in candidates)
        assert ids == ["s2"]
        assert stats["skipped_already_distilled"] == 1

    def test_force_mode_includes_already_distilled(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12)
        d = _write_distillation(vault, linked_session_id="s1")
        vault.write(d)
        candidates, _ = find_session_candidates(
            vault, recency_days=None, include_already_distilled=True
        )
        assert [c.session_id for c in candidates] == ["s1"]

    def test_below_threshold_sessions_skipped_and_counted(self, vault: Vault) -> None:
        _write_session(vault, session_id="ok", turns=12)
        _write_session(vault, session_id="too_short", turns=4)
        candidates, stats = find_session_candidates(vault, recency_days=None)
        assert [c.session_id for c in candidates] == ["ok"]
        assert stats["skipped_too_short"] == 1

    def test_recency_window_filters_old_sessions(self, vault: Vault) -> None:
        now = datetime(2026, 5, 1, tzinfo=UTC)
        _write_session(vault, session_id="recent", turns=12, updated=now - timedelta(days=5))
        _write_session(vault, session_id="old", turns=12, updated=now - timedelta(days=90))
        candidates, _ = find_session_candidates(vault, recency_days=30, now=now)
        assert [c.session_id for c in candidates] == ["recent"]

    def test_backfill_mode_ignores_recency(self, vault: Vault) -> None:
        now = datetime(2026, 5, 1, tzinfo=UTC)
        _write_session(vault, session_id="recent", turns=12, updated=now - timedelta(days=5))
        _write_session(vault, session_id="old", turns=12, updated=now - timedelta(days=90))
        candidates, _ = find_session_candidates(vault, recency_days=None, now=now)
        ids = sorted(c.session_id for c in candidates)
        assert ids == ["old", "recent"]

    def test_candidate_carries_agent_tag_when_present(self, vault: Vault) -> None:
        _write_session(
            vault,
            session_id="s1",
            turns=12,
            tags=["agent:ari", "topic:foo"],
            source="openclaw",
        )
        candidates, _ = find_session_candidates(vault, recency_days=None)
        assert candidates[0].agent == "ari"


def test_find_distilled_session_ids_handles_empty_vault(vault: Vault) -> None:
    assert find_distilled_session_ids(vault) == set()


def test_find_distilled_session_ids_extracts_from_links(vault: Vault) -> None:
    d = _write_distillation(vault, linked_session_id="abc-123")
    vault.write(d)
    ids = find_distilled_session_ids(vault)
    assert ids == {"abc-123"}


def test_find_distilled_session_ids_ignores_non_session_links(vault: Vault) -> None:
    """Frontmatter links may carry unrelated targets — those shouldn't pollute the set."""
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "type": "distillation",
        "created": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "updated": datetime(2026, 4, 28, tzinfo=UTC).isoformat(),
        "source": "hygiene-worker",
        "title": "topic distillation",
        "tags": ["distillation:topic"],
        "links": [
            "memory://memories/some-memory",  # not a session
            "memory://sessions/legit-session",
            "Wiki Reference",
        ],
        "importance": 0.7,
    }
    fm = validate(payload)
    m = Memory(frontmatter=fm, body="x", path=Path("distillations/topic/x.md"))
    vault.write(m)
    assert find_distilled_session_ids(vault) == {"legit-session"}


# ─── Prompt construction ──────────────────────────────────────────


def _make_candidate(**overrides: object) -> SessionCandidate:
    base: dict[str, object] = {
        "memory_id": "mem-id",
        "title": "Title",
        "body": "Body content",
        "tags": ["agent:ari", "home-ubuntu-woodfield-quotes"],
        "source": "claude-code",
        "agent": None,
        "session_id": "sess-id",
        "turn_count": 12,
        "word_count": 200,
        "created": datetime(2026, 4, 28, tzinfo=UTC),
        "updated": datetime(2026, 4, 28, tzinfo=UTC),
    }
    base.update(overrides)
    return SessionCandidate(**base)  # type: ignore[arg-type]


def test_build_session_prompt_interpolates_title_tags_body() -> None:
    candidate = _make_candidate(
        title="Woodfield Country Club e-bike proposal",
        tags=["agent:ari", "home-ubuntu-woodfield-quotes"],
        body="**User:** can you build a proposal\n\n**Assistant:** sure",
    )
    prompt = build_session_prompt(candidate)
    assert "Woodfield Country Club e-bike proposal" in prompt
    assert "agent:ari, home-ubuntu-woodfield-quotes" in prompt
    assert "can you build a proposal" in prompt


def test_build_session_prompt_handles_no_tags() -> None:
    candidate = _make_candidate(tags=[])
    prompt = build_session_prompt(candidate)
    assert "(none)" in prompt


def test_build_session_prompt_accepts_template_override() -> None:
    template = "T={title}\nG={tags}\nB={body}"
    candidate = _make_candidate(title="X", tags=["a"], body="b")
    prompt = build_session_prompt(candidate, prompt_template=template)
    assert prompt == "T=X\nG=a\nB=b"


# ─── Materialization ──────────────────────────────────────────────


class TestMaterializeDistillation:
    def test_basic_shape(self) -> None:
        candidate = _make_candidate(session_id="abc", source="claude-code", agent=None)
        memory = materialize_distillation(
            candidate,
            summary="## Summary\n\nbody",
            summarizer_name="stub",
        )
        assert memory.type is MemoryType.DISTILLATION
        assert memory.body == "## Summary\n\nbody"
        assert memory.path == Path("distillations/claude-code/abc.md")

    def test_links_back_to_source_session(self) -> None:
        candidate = _make_candidate(session_id="abc")
        memory = materialize_distillation(candidate, summary="x", summarizer_name="stub")
        assert memory.frontmatter.links == ["memory://sessions/abc"]

    def test_provenance_ref_uses_session_id(self) -> None:
        candidate = _make_candidate(session_id="abc")
        memory = materialize_distillation(candidate, summary="x", summarizer_name="stub")
        prov = memory.frontmatter.provenance
        assert prov is not None
        assert prov.ref == f"{PROVENANCE_REF_PREFIX}abc"
        # Summarizer is captured via Provenance's extra="allow".
        prov_extra = getattr(prov, "model_extra", None) or {}
        assert prov_extra.get("summarizer") == "stub"

    def test_inherits_tags_and_adds_distillation_marker(self) -> None:
        candidate = _make_candidate(tags=["agent:ari", "home-ubuntu-woodfield-quotes"])
        memory = materialize_distillation(candidate, summary="x", summarizer_name="stub")
        assert "agent:ari" in memory.frontmatter.tags
        assert "home-ubuntu-woodfield-quotes" in memory.frontmatter.tags
        assert DISTILLATION_KIND_TAG in memory.frontmatter.tags

    def test_default_importance_is_seed(self) -> None:
        candidate = _make_candidate()
        memory = materialize_distillation(candidate, summary="x", summarizer_name="stub")
        assert memory.frontmatter.importance == DEFAULT_DISTILLATION_IMPORTANCE

    def test_importance_override_honored(self) -> None:
        candidate = _make_candidate()
        memory = materialize_distillation(
            candidate, summary="x", summarizer_name="stub", importance=0.95
        )
        assert memory.frontmatter.importance == 0.95

    def test_agent_in_path_when_present(self) -> None:
        candidate = _make_candidate(source="openclaw", agent="ari", session_id="xyz")
        memory = materialize_distillation(candidate, summary="x", summarizer_name="stub")
        assert memory.path == Path("distillations/openclaw/ari/xyz.md")

    def test_explicit_memory_id_preserved_for_force_overwrite(self) -> None:
        candidate = _make_candidate()
        explicit = "11111111-1111-1111-1111-111111111111"
        memory = materialize_distillation(
            candidate, summary="x", summarizer_name="stub", memory_id=explicit
        )
        assert str(memory.id) == explicit


# ─── Plan + apply ─────────────────────────────────────────────────


class TestComputeDistillationPlan:
    def test_empty_vault_yields_empty_plan(self, vault: Vault) -> None:
        plan = compute_distillation_plan(vault, NoOpSummarizer(), recency_days=None)
        assert plan.proposals == []

    def test_noop_summarizer_marks_proposals_as_empty_with_reason(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12)
        plan = compute_distillation_plan(vault, NoOpSummarizer(), recency_days=None)
        assert len(plan.proposals) == 1
        prop = plan.proposals[0]
        assert prop.summary == ""
        assert prop.skipped_reason is not None
        assert "NoOp" in prop.skipped_reason or "empty" in prop.skipped_reason

    def test_stub_summarizer_produces_summaries(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12, title="The Woodfield work")
        stub = StubSummarizer()
        stub.set_default("## Summary\n\nWoodfield demo video work.")
        plan = compute_distillation_plan(vault, stub, recency_days=None)
        assert len(plan.proposals) == 1
        prop = plan.proposals[0]
        assert "Woodfield" in prop.summary
        assert prop.summarizer_name == "stub"
        assert prop.skipped_reason is None

    def test_force_includes_already_distilled(self, vault: Vault) -> None:
        _write_session(vault, session_id="s1", turns=12)
        d = _write_distillation(vault, linked_session_id="s1")
        vault.write(d)
        stub = StubSummarizer()
        stub.set_default("re-summarized")
        plan = compute_distillation_plan(vault, stub, recency_days=None, force=True)
        assert len(plan.proposals) == 1


class TestApplyDistillations:
    def test_apply_writes_distillation_files_and_indexes_them(
        self, vault: Vault, index: Index
    ) -> None:
        _write_session(vault, session_id="s1", turns=12, title="x")
        stub = StubSummarizer()
        stub.set_default("## Summary\n\nbody")
        plan = compute_distillation_plan(vault, stub, recency_days=None)
        result = apply_distillations(vault, index, plan)
        assert result.written == 1
        assert result.apply_errors == []
        # Confirm the distillation file exists in the vault.
        distillations = list(vault.walk(types=[MemoryType.DISTILLATION.value]))
        assert len(distillations) == 1
        d = distillations[0]
        assert d.body == "## Summary\n\nbody"
        assert d.frontmatter.links == ["memory://sessions/s1"]
        # And confirm the index has a row.
        rows = index.db.execute(
            "SELECT id, type FROM memories WHERE id = ?", (str(d.id),)
        ).fetchone()
        assert rows is not None
        assert rows["type"] == MemoryType.DISTILLATION.value
        # And confirm the distillation was enqueued for embedding so
        # vec retrieval can find it. Without this, BM25 alone has the
        # long source transcript outranking the focused summary.
        queue_row = index.db.execute(
            "SELECT memory_id FROM embed_queue WHERE memory_id = ?", (str(d.id),)
        ).fetchone()
        assert queue_row is not None

    def test_apply_skips_proposals_with_empty_summary(self, vault: Vault, index: Index) -> None:
        _write_session(vault, session_id="s1", turns=12)
        plan = compute_distillation_plan(vault, NoOpSummarizer(), recency_days=None)
        result = apply_distillations(vault, index, plan)
        assert result.written == 0
        assert result.skipped_no_summary == 1
        assert list(vault.walk(types=[MemoryType.DISTILLATION.value])) == []

    def test_force_overwrite_preserves_existing_memory_id(self, vault: Vault, index: Index) -> None:
        _write_session(vault, session_id="s1", turns=12)
        # First pass.
        stub = StubSummarizer()
        stub.set_default("first")
        plan = compute_distillation_plan(vault, stub, recency_days=None)
        apply_distillations(vault, index, plan)
        first = next(iter(vault.walk(types=[MemoryType.DISTILLATION.value])))
        original_id = first.id
        # Second pass with --force and a different output.
        stub.set_default("second pass")
        plan2 = compute_distillation_plan(vault, stub, recency_days=None, force=True)
        apply_distillations(vault, index, plan2)
        second = next(iter(vault.walk(types=[MemoryType.DISTILLATION.value])))
        # Same memory_id (overwrite, not duplicate) and refreshed body.
        assert second.id == original_id
        assert second.body == "second pass"

    def test_apply_is_resilient_to_per_proposal_failure(
        self, vault: Vault, index: Index, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_session(vault, session_id="s1", turns=12, title="ok")
        _write_session(vault, session_id="s2", turns=12, title="will-fail")
        stub = StubSummarizer()
        stub.set_default("body")
        plan = compute_distillation_plan(vault, stub, recency_days=None)

        # Patch vault.write to fail on the second proposal only.
        from memstem.core import storage as storage_module

        original_write = storage_module.Vault.write
        call_count = {"n": 0}

        def flaky_write(self: Vault, memory: Memory) -> None:
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("disk full")
            original_write(self, memory)

        monkeypatch.setattr(storage_module.Vault, "write", flaky_write)
        result = apply_distillations(vault, index, plan)
        # One succeeded, one failed; no crash; error captured.
        assert result.written == 1
        assert len(result.apply_errors) == 1
        assert "disk full" in result.apply_errors[0]


# ─── CLI ──────────────────────────────────────────────────────────


class TestCli:
    def _setup_vault_with_session(self, tmp_path: Path) -> Path:
        vault_path = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "distillations", "_meta"):
            (vault_path / sub).mkdir(parents=True, exist_ok=True)
        v = Vault(vault_path)
        _write_session(v, session_id="cli-1", turns=12, title="Woodfield e-bike work")
        # Write minimal config so cli's _load_config picks the vault path up.
        cfg = {
            "vault_path": str(vault_path),
            "embedding": {"provider": "ollama"},
        }
        import yaml as _yaml

        (vault_path / "_meta" / "config.yaml").write_text(_yaml.safe_dump(cfg), encoding="utf-8")
        return vault_path

    def test_cli_dry_run_with_noop_provider(self, tmp_path: Path) -> None:
        vault_path = self._setup_vault_with_session(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "hygiene",
                "distill-sessions",
                "--vault",
                str(vault_path),
                "--backfill",
                "--provider",
                "noop",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "scanned: 1 session record(s)" in result.output
        assert "dry-run" in result.output
        # NoOp produces no proposals worth applying — but the candidate
        # should still appear in the listing.
        assert "cli-1" in result.output

    def test_cli_unknown_provider_errors(self, tmp_path: Path) -> None:
        vault_path = self._setup_vault_with_session(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "hygiene",
                "distill-sessions",
                "--vault",
                str(vault_path),
                "--provider",
                "voodoo",
            ],
        )
        assert result.exit_code == 2
        assert "unknown provider" in result.output
