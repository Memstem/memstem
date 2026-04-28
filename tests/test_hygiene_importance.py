"""Tests for `memstem.hygiene.importance` (ADR 0008 PR-C).

Cover: dry-run plan vs apply, idempotence via the cursor, the per-row
weight formula, the cap on per-run bumps, the cap at 1.0, the skip
rules for expired/deprecated records, and the CLI subcommand.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.retrieval_log import LoggedHit, log_get, log_search_results
from memstem.core.storage import Memory, Vault
from memstem.hygiene.importance import (
    CURSOR_KEY,
    GET_WEIGHT,
    MAX_BUMP_PER_RUN,
    SEARCH_WEIGHT_ROOT,
    apply_importance_updates,
    compute_importance_updates,
    reset_cursor,
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


def _make_memory(
    *,
    body: str,
    vault: Vault,
    importance: float | None = None,
    valid_to: datetime | None = None,
    deprecated_by: UUID | None = None,
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": "memory",
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": "test",
        "tags": [],
    }
    if importance is not None:
        metadata["importance"] = importance
    if valid_to is not None:
        metadata["valid_to"] = valid_to.isoformat()
    if deprecated_by is not None:
        metadata["deprecated_by"] = str(deprecated_by)
    fm = validate(metadata)
    memory = Memory(frontmatter=fm, body=body, path=Path(f"memories/{fm.id}.md"))
    vault.write(memory)
    return memory


class TestComputeImportanceUpdates:
    """`compute_importance_updates` is the pure planner — no side effects."""

    def test_no_log_rows_returns_empty(self, vault: Vault, index: Index) -> None:
        plan = compute_importance_updates(vault, index)
        assert plan.updates == []
        assert plan.last_seen_id == 0

    def test_search_exposure_at_rank_one_proposes_bump(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_search_results(
            index.db,
            query="alpha",
            hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
            client="cli",
        )
        plan = compute_importance_updates(vault, index)
        assert len(plan.updates) == 1
        update = plan.updates[0]
        assert update.memory_id == str(m.id)
        # rank-1 search at full recency = SEARCH_WEIGHT_ROOT * 1.0
        assert update.proposed == pytest.approx(0.5 + SEARCH_WEIGHT_ROOT)

    def test_get_exposure_proposes_larger_bump(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        assert len(plan.updates) == 1
        update = plan.updates[0]
        # GET_WEIGHT (0.05) > SEARCH_WEIGHT_ROOT (0.01)
        assert update.proposed == pytest.approx(0.5 + GET_WEIGHT)

    def test_repeated_exposure_accumulates_until_cap(self, vault: Vault, index: Index) -> None:
        # Many gets pile up but the per-run cap kicks in.
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        for _ in range(20):
            log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        update = plan.updates[0]
        # Raw bump = 20 * 0.05 = 1.0; capped by MAX_BUMP_PER_RUN.
        assert update.proposed == pytest.approx(0.5 + MAX_BUMP_PER_RUN)

    def test_caps_at_one_point_zero(self, vault: Vault, index: Index) -> None:
        # importance already 0.95; even a 0.1 bump can only raise it to 1.0.
        m = _make_memory(body="alpha", vault=vault, importance=0.95)
        index.upsert(m)
        for _ in range(20):
            log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        assert plan.updates[0].proposed == pytest.approx(1.0)

    def test_skips_already_at_cap(self, vault: Vault, index: Index) -> None:
        # importance == 1.0: no further bumps can apply.
        m = _make_memory(body="alpha", vault=vault, importance=1.0)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        assert plan.updates == []

    def test_skips_expired_records(self, vault: Vault, index: Index) -> None:
        past = datetime.now(tz=UTC) - timedelta(days=1)
        m = _make_memory(body="alpha", vault=vault, importance=0.5, valid_to=past)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        assert plan.updates == []

    def test_skips_deprecated_records(self, vault: Vault, index: Index) -> None:
        m = _make_memory(
            body="alpha",
            vault=vault,
            importance=0.5,
            deprecated_by=uuid4(),
        )
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        assert plan.updates == []

    def test_unset_importance_uses_neutral_default(self, vault: Vault, index: Index) -> None:
        # No frontmatter.importance → treat as 0.5 (matches Search.search default).
        m = _make_memory(body="alpha", vault=vault, importance=None)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        update = plan.updates[0]
        assert update.current == pytest.approx(0.5)
        assert update.proposed == pytest.approx(0.5 + GET_WEIGHT)

    def test_old_exposure_weighted_at_half(self, vault: Vault, index: Index) -> None:
        # An exposure 90 days old should weight at 0.5 (recency penalty).
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        old_ts = datetime.now(tz=UTC) - timedelta(days=90)
        log_search_results(
            index.db,
            query="alpha",
            hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
            client="cli",
            now=old_ts,
        )
        plan = compute_importance_updates(vault, index)
        update = plan.updates[0]
        assert update.proposed == pytest.approx(0.5 + SEARCH_WEIGHT_ROOT * 0.5)

    def test_pure_planner_does_not_advance_cursor(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        compute_importance_updates(vault, index)
        # Cursor should still be unset (default 0).
        row = index.db.execute(
            "SELECT value FROM hygiene_state WHERE key = ?", (CURSOR_KEY,)
        ).fetchone()
        assert row is None


class TestApplyImportanceUpdates:
    def test_apply_writes_to_vault_and_index(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")

        plan = compute_importance_updates(vault, index)
        n = apply_importance_updates(vault, index, plan)
        assert n == 1

        # Vault re-read shows the new importance.
        re_read = vault.read(m.path)
        assert re_read.frontmatter.importance == pytest.approx(0.5 + GET_WEIGHT)

        # Index column also reflects it.
        row = index.db.execute(
            "SELECT importance FROM memories WHERE id = ?", (str(m.id),)
        ).fetchone()
        assert row["importance"] == pytest.approx(0.5 + GET_WEIGHT)

    def test_apply_advances_cursor(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")

        plan = compute_importance_updates(vault, index)
        apply_importance_updates(vault, index, plan)
        row = index.db.execute(
            "SELECT value FROM hygiene_state WHERE key = ?", (CURSOR_KEY,)
        ).fetchone()
        assert row is not None
        assert int(row["value"]) == plan.last_seen_id

    def test_re_apply_is_no_op(self, vault: Vault, index: Index) -> None:
        # Hit once, apply, then immediately rerun: no new updates.
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan_a = compute_importance_updates(vault, index)
        apply_importance_updates(vault, index, plan_a)
        # Second sweep: nothing left to bump.
        plan_b = compute_importance_updates(vault, index)
        assert plan_b.updates == []

    def test_new_log_rows_after_apply_proposed_again(self, vault: Vault, index: Index) -> None:
        # Apply, then add new log rows: those should produce a new
        # plan referencing the same memory.
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan_a = compute_importance_updates(vault, index)
        apply_importance_updates(vault, index, plan_a)
        # New retrieval after the cursor advanced.
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan_b = compute_importance_updates(vault, index)
        assert len(plan_b.updates) == 1
        # Importance is now ~0.55 → second bump goes to ~0.60.
        assert plan_b.updates[0].current == pytest.approx(0.5 + GET_WEIGHT)
        assert plan_b.updates[0].proposed == pytest.approx(0.5 + GET_WEIGHT + GET_WEIGHT)

    def test_empty_plan_still_advances_cursor(self, vault: Vault, index: Index) -> None:
        # Even when no updates apply (e.g., all skipped or capped), the
        # cursor should advance so the next sweep doesn't re-scan the
        # same window.
        m = _make_memory(body="alpha", vault=vault, importance=1.0)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        # Plan is empty (capped record) but last_seen_id is set.
        assert plan.updates == []
        assert plan.last_seen_id > 0
        apply_importance_updates(vault, index, plan)
        row = index.db.execute(
            "SELECT value FROM hygiene_state WHERE key = ?", (CURSOR_KEY,)
        ).fetchone()
        assert int(row["value"]) == plan.last_seen_id

    def test_record_deleted_between_plan_and_apply_is_skipped(
        self, vault: Vault, index: Index
    ) -> None:
        # Plan a bump for a memory, then delete it from the index, then
        # apply. The apply should not crash and should not produce a
        # bump.
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan = compute_importance_updates(vault, index)
        index.delete(str(m.id))
        n = apply_importance_updates(vault, index, plan)
        assert n == 0


class TestResetCursor:
    def test_reset_cursor_lets_next_plan_re_scan(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="alpha", vault=vault, importance=0.5)
        index.upsert(m)
        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        plan_a = compute_importance_updates(vault, index)
        apply_importance_updates(vault, index, plan_a)

        reset_cursor(index)
        # With cursor reset, the same row is re-considered.
        plan_b = compute_importance_updates(vault, index)
        assert len(plan_b.updates) == 1


class TestHygieneCli:
    """`memstem hygiene importance` subcommand smoke tests."""

    def _vault_with_meta(self, tmp_path: Path) -> Path:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        # Stub config so _load_config doesn't try to construct a default.
        (root / "_meta" / "config.yaml").write_text(f"vault_path: {root}\n", encoding="utf-8")
        return root

    def test_dry_run_does_not_mutate(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        idx = Index(root / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            m = _make_memory(body="alpha", vault=v, importance=0.5)
            idx.upsert(m)
            log_get(idx.db, memory_id=str(m.id), client="mcp:get")
        finally:
            idx.close()

        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "importance", "--vault", str(root)])
        assert result.exit_code == 0, result.stdout
        # Look for the dry-run banner and the proposed bump line.
        assert "dry-run" in result.stdout
        assert "0.500" in result.stdout
        # Re-read the memory: it must NOT have been mutated.
        re_read = v.read(m.path)
        assert re_read.frontmatter.importance == pytest.approx(0.5)

    def test_apply_persists_and_reports_count(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        idx = Index(root / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            m = _make_memory(body="alpha", vault=v, importance=0.5)
            idx.upsert(m)
            log_get(idx.db, memory_id=str(m.id), client="mcp:get")
        finally:
            idx.close()

        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "importance", "--apply", "--vault", str(root)])
        assert result.exit_code == 0, result.stdout
        assert "applied 1 bump" in result.stdout
        # Memory now has bumped importance on disk.
        re_read = v.read(m.path)
        assert re_read.frontmatter.importance == pytest.approx(0.5 + GET_WEIGHT)

    def test_no_log_rows_prints_nothing_to_apply(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "importance", "--vault", str(root)])
        assert result.exit_code == 0
        assert "no bumps proposed" in result.stdout
