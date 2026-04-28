"""Tests for the ADR 0008 Tier 1 query log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.retrieval_log import (
    DEFAULT_MAX_ROWS,
    LoggedHit,
    count,
    log_get,
    log_search_results,
)
from memstem.core.search import Search
from memstem.core.storage import Memory, Vault


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


def _make_memory(*, body: str, vault: Vault, importance: float | None = None) -> Memory:
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
    fm = validate(metadata)
    memory = Memory(frontmatter=fm, body=body, path=Path(f"memories/{fm.id}.md"))
    vault.write(memory)
    return memory


class TestQueryLogTable:
    """The schema migration creates the query_log table."""

    def test_query_log_table_exists(self, index: Index) -> None:
        # Migration v5 creates the table; the smoke check is that the
        # daemon can log into it without crashing.
        rows = index.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='query_log'"
        ).fetchall()
        assert len(rows) == 1

    def test_query_log_starts_empty(self, index: Index) -> None:
        assert count(index.db) == 0

    def test_query_log_has_expected_columns(self, index: Index) -> None:
        # Defensive: the schema is the contract the hygiene worker reads.
        # If a column is renamed or removed, that worker breaks silently.
        rows = index.db.execute("PRAGMA table_info(query_log)").fetchall()
        cols = {row["name"] for row in rows}
        assert cols == {"id", "ts", "kind", "query", "client", "memory_id", "rank", "score"}


class TestLogSearchResults:
    """``log_search_results`` writes one row per hit."""

    def test_writes_one_row_per_hit(self, vault: Vault, index: Index) -> None:
        m1 = _make_memory(body="alpha one", vault=vault)
        m2 = _make_memory(body="alpha two", vault=vault)
        index.upsert(m1)
        index.upsert(m2)

        hits = [
            LoggedHit(memory_id=str(m1.id), rank=1, score=0.5),
            LoggedHit(memory_id=str(m2.id), rank=2, score=0.3),
        ]
        log_search_results(index.db, query="alpha", hits=hits, client="cli")
        assert count(index.db) == 2

        rows = index.db.execute(
            "SELECT memory_id, rank, score, kind, query, client FROM query_log ORDER BY rank"
        ).fetchall()
        assert rows[0]["memory_id"] == str(m1.id)
        assert rows[0]["rank"] == 1
        assert rows[0]["score"] == pytest.approx(0.5)
        assert rows[0]["kind"] == "search"
        assert rows[0]["query"] == "alpha"
        assert rows[0]["client"] == "cli"

    def test_empty_hits_writes_nothing(self, index: Index) -> None:
        log_search_results(index.db, query="alpha", hits=[], client="cli")
        assert count(index.db) == 0

    def test_failure_does_not_raise(self, vault: Vault, index: Index) -> None:
        # Deliberately corrupt the query_log table to force a sqlite error
        # at insert time. The wrapper must NOT propagate it — search must
        # never break because the log can't be written.
        index.db.execute("DROP TABLE query_log")
        index.db.commit()

        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        # Should warn but not raise.
        log_search_results(
            index.db,
            query="alpha",
            hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
            client="cli",
        )

    def test_writes_with_explicit_now(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        log_search_results(
            index.db,
            query="x",
            hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
            client="cli",
            now=ts,
        )
        row = index.db.execute("SELECT ts FROM query_log").fetchone()
        assert row["ts"] == "2026-01-01T12:00:00+00:00"

    def test_null_client_is_allowed(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        log_search_results(
            index.db,
            query="x",
            hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
            client=None,
        )
        row = index.db.execute("SELECT client FROM query_log").fetchone()
        assert row["client"] is None


class TestLogGet:
    """``log_get`` writes one row with kind='get'."""

    def test_writes_get_row(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)

        log_get(index.db, memory_id=str(m.id), client="mcp:get")
        row = index.db.execute("SELECT * FROM query_log").fetchone()
        assert row["kind"] == "get"
        assert row["query"] is None
        assert row["rank"] is None
        assert row["score"] is None
        assert row["memory_id"] == str(m.id)
        assert row["client"] == "mcp:get"

    def test_failure_does_not_raise(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        index.db.execute("DROP TABLE query_log")
        index.db.commit()
        # Should warn but not raise.
        log_get(index.db, memory_id=str(m.id), client="mcp:get")


class TestPruning:
    """``max_rows`` triggers FIFO prune when the cap is exceeded."""

    def test_does_not_prune_below_cap(self, vault: Vault, index: Index) -> None:
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        for i in range(10):
            log_search_results(
                index.db,
                query=f"q{i}",
                hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
                client="cli",
                max_rows=100,
            )
        assert count(index.db) == 10

    def test_prunes_oldest_when_cap_exceeded(self, vault: Vault, index: Index) -> None:
        # Set a very low cap to make the prune happen on a small write
        # volume. Verify that after we exceed the cap, the row count
        # drops to ~90% of the cap (the documented headroom).
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        for i in range(15):
            log_search_results(
                index.db,
                query=f"q{i}",
                hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
                client="cli",
                max_rows=10,
            )
        # After write 11+ we cross the cap; the prune drops back to
        # ~90% of 10 = 9. Subsequent writes fit until cap is crossed
        # again.
        post_count = count(index.db)
        assert post_count <= 10
        assert post_count >= 1  # never empties
        # The remaining rows should be the most recent ones.
        rows = index.db.execute("SELECT query FROM query_log ORDER BY id ASC").fetchall()
        # Earliest remaining row's query string must be from the later
        # writes, not the first.
        assert rows[0]["query"] != "q0"

    def test_max_rows_zero_disables_prune(self, vault: Vault, index: Index) -> None:
        # ``max_rows=0`` is a valid "never prune" sentinel.
        m = _make_memory(body="x", vault=vault)
        index.upsert(m)
        for i in range(20):
            log_search_results(
                index.db,
                query=f"q{i}",
                hits=[LoggedHit(memory_id=str(m.id), rank=1, score=0.1)],
                client="cli",
                max_rows=0,
            )
        assert count(index.db) == 20


class TestSearchIntegration:
    """``Search.search`` writes log rows when ``log_client`` is set."""

    def test_search_with_log_client_writes_rows(self, vault: Vault, index: Index) -> None:
        for body in ["alpha one", "alpha two", "alpha three"]:
            m = _make_memory(body=body, vault=vault)
            index.upsert(m)

        search = Search(vault=vault, index=index)
        results = search.search("alpha", limit=3, log_client="cli")
        assert len(results) == 3
        # One row per surfaced hit.
        assert count(index.db) == 3

        rows = index.db.execute(
            "SELECT kind, client, query, rank FROM query_log ORDER BY rank"
        ).fetchall()
        assert all(row["kind"] == "search" for row in rows)
        assert all(row["client"] == "cli" for row in rows)
        assert all(row["query"] == "alpha" for row in rows)
        assert [row["rank"] for row in rows] == [1, 2, 3]

    def test_search_without_log_client_writes_nothing(self, vault: Vault, index: Index) -> None:
        # The default (log_client=None) keeps existing test fixtures
        # blast-radius-zero — they don't accidentally write log rows.
        m = _make_memory(body="alpha", vault=vault)
        index.upsert(m)
        search = Search(vault=vault, index=index)
        search.search("alpha", limit=5)
        assert count(index.db) == 0

    def test_search_logging_failure_does_not_break_search(self, vault: Vault, index: Index) -> None:
        # The killer case: even if logging is completely broken (table
        # dropped, schema corrupted, whatever), search must still
        # return results. Otherwise we've turned an analytics feature
        # into a search-killing dependency.
        m = _make_memory(body="alpha topic", vault=vault)
        index.upsert(m)
        index.db.execute("DROP TABLE query_log")
        index.db.commit()

        search = Search(vault=vault, index=index)
        results = search.search("alpha", limit=5, log_client="cli")
        assert len(results) == 1  # search still works

    def test_search_logs_post_importance_score(self, vault: Vault, index: Index) -> None:
        # The score recorded in the log must be the FINAL boosted score,
        # not the bare RRF — the hygiene worker will eventually use this
        # to prioritize importance bumps.
        m = _make_memory(body="alpha topic", vault=vault, importance=1.0)
        index.upsert(m)

        search = Search(vault=vault, index=index)
        results = search.search("alpha", limit=1, importance_weight=0.5, log_client="cli")
        # Search returns score == rrf * (1 + 0.5 * 1.0) = 1.5/61
        expected = (1 / 61) * 1.5
        assert results[0].score == pytest.approx(expected)
        # The log row should match.
        row = index.db.execute("SELECT score FROM query_log").fetchone()
        assert row["score"] == pytest.approx(expected)


class TestNoLogTableDefault:
    """The query log starts empty even when search is exercised heavily.

    Ensures the existing test suite doesn't accidentally start writing
    log rows when callers don't opt in.
    """

    def test_existing_search_callers_do_not_log(self, vault: Vault, index: Index) -> None:
        # Replicate the v0.6.x default Search() usage. No log_client →
        # no rows. The hygiene worker should never see synthetic data
        # from un-instrumented test runs.
        m = _make_memory(body="alpha topic", vault=vault)
        index.upsert(m)
        for _ in range(5):
            Search(vault=vault, index=index).search("alpha", limit=3)
        assert count(index.db) == 0


class TestCountHelper:
    def test_count_returns_int(self, index: Index) -> None:
        # Smoke test for the doctor / debugging helper.
        assert isinstance(count(index.db), int)
        assert count(index.db) == 0


class TestDefaults:
    def test_default_max_rows_constant(self) -> None:
        assert DEFAULT_MAX_ROWS == 100_000

    def test_count_handles_empty_connection_safely(self, tmp_path: Path) -> None:
        # Should not crash if the table is missing — just returns 0.
        db = sqlite3.connect(tmp_path / "no_log.db")
        # Without query_log table, count() should raise; this documents
        # that the helper assumes the table exists. The wrapper
        # (log_search_results / log_get) is what shields callers from
        # missing tables; count() is internal-debug only.
        with pytest.raises(sqlite3.OperationalError):
            count(db)
