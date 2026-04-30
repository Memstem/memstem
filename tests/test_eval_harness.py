"""Tests for the recall-quality eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from memstem.core.frontmatter import Frontmatter, validate
from memstem.core.index import Index
from memstem.core.search import Result, Search
from memstem.core.storage import Memory, Vault
from memstem.eval import (
    EvalQuery,
    ExpectMatcher,
    QueryResult,
    format_report,
    load_queries,
    report_to_json,
    run_eval,
    run_query,
)

# ─── ExpectMatcher ────────────────────────────────────────────────────


def _build_result(
    *,
    title: str = "",
    body: str = "",
    path: str = "memories/test.md",
    score: float = 0.5,
) -> Result:
    """Build a synthetic Result for matcher unit tests."""
    fm: Frontmatter = validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "type": "memory",
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z",
            "source": "test",
            "title": title,
        }
    )
    memory = Memory(frontmatter=fm, body=body, path=Path(path))
    return Result(memory=memory, score=score, bm25_rank=1, vec_rank=None)


def test_expect_matcher_title_substring_case_insensitive() -> None:
    matcher = ExpectMatcher(title_contains=("Memory Architecture",))
    assert matcher.matches(_build_result(title="Ari Memory Architecture v3"))
    assert matcher.matches(_build_result(title="ARI MEMORY ARCHITECTURE V3"))
    assert not matcher.matches(_build_result(title="Other Title"))


def test_expect_matcher_body_substring() -> None:
    matcher = ExpectMatcher(body_contains=("18789",))
    assert matcher.matches(_build_result(body="port 18789 is the gateway"))
    assert not matcher.matches(_build_result(body="port 18790 something else"))


def test_expect_matcher_path_substring() -> None:
    matcher = ExpectMatcher(path_contains=("CLAUDE.md",))
    assert matcher.matches(_build_result(path="memories/claude-code/CLAUDE.md"))
    assert not matcher.matches(_build_result(path="memories/openclaw/something.md"))


def test_expect_matcher_logical_or_across_fields() -> None:
    matcher = ExpectMatcher(title_contains=("foo",), body_contains=("bar",))
    assert matcher.matches(_build_result(title="foo only"))
    assert matcher.matches(_build_result(body="bar only"))
    assert matcher.matches(_build_result(title="foo", body="bar"))
    assert not matcher.matches(_build_result(title="other", body="other"))


def test_expect_matcher_empty_never_matches() -> None:
    matcher = ExpectMatcher()
    assert not matcher.matches(_build_result(title="anything", body="anything"))


# ─── load_queries ─────────────────────────────────────────────────────


def test_load_queries_round_trip(tmp_path: Path) -> None:
    yaml_text = """
queries:
  - id: q1
    class: factual
    query: what is the answer
    expect:
      body_contains: ["42"]
  - id: q2
    class: conceptual
    query: how does this work
    expect:
      title_contains: ["overview"]
      path_contains: ["overview.md"]
    top_k: 5
"""
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text(yaml_text)
    queries = load_queries(queries_file)
    assert len(queries) == 2
    assert queries[0].id == "q1"
    assert queries[0].class_ == "factual"
    assert queries[0].expect.body_contains == ("42",)
    assert queries[0].top_k == 10  # default
    assert queries[1].top_k == 5
    assert queries[1].expect.title_contains == ("overview",)
    assert queries[1].expect.path_contains == ("overview.md",)


def test_load_queries_invalid_class(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text(
        """
queries:
  - id: bad
    class: bogus
    query: anything
    expect:
      body_contains: ["x"]
"""
    )
    with pytest.raises(ValueError, match="class must be one of"):
        load_queries(queries_file)


def test_load_queries_missing_query(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text(
        """
queries:
  - id: bad
    class: factual
    expect:
      body_contains: ["x"]
"""
    )
    with pytest.raises(ValueError, match="query string is required"):
        load_queries(queries_file)


def test_load_queries_empty_expect_rejected(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text(
        """
queries:
  - id: bad
    class: factual
    query: anything
    expect: {}
"""
    )
    with pytest.raises(ValueError, match="must specify at least one"):
        load_queries(queries_file)


def test_load_queries_invalid_top_k(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text(
        """
queries:
  - id: bad
    class: factual
    query: anything
    expect:
      body_contains: ["x"]
    top_k: 0
"""
    )
    with pytest.raises(ValueError, match="top_k must be >= 1"):
        load_queries(queries_file)


def test_load_queries_top_level_not_list(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text("queries: not_a_list\n")
    with pytest.raises(ValueError, match="expected list"):
        load_queries(queries_file)


def test_load_queries_empty_file(tmp_path: Path) -> None:
    queries_file = tmp_path / "queries.yaml"
    queries_file.write_text("")
    queries = load_queries(queries_file)
    assert queries == []


# ─── run_query / run_eval (BM25 only — no embedder needed) ────────────


@pytest.fixture
def fixture_search(tmp_path: Path) -> Search:
    """Build a small but real Vault + Index with five memories.

    Uses BM25 only (no embedder) so the test doesn't need Ollama.
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    index_path = vault_root / "_meta" / "index.db"
    index_path.parent.mkdir(parents=True)
    index = Index(index_path)
    index.connect()
    vault = Vault(vault_root)

    fixtures = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "type": "memory",
            "title": "Server Environment Reference",
            "body": "Ari runs on port 18789. Fleet runs on port 3021.",
            "path": "memories/server-env.md",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "type": "skill",
            "title": "Send Telegram",
            "body": "Use bash ~/scripts/tg-send to message Brad.",
            "path": "skills/send-telegram.md",
        },
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "type": "memory",
            "title": "Cloudflare Migration",
            "body": "Move all new domains to Cloudflare for at-cost pricing.",
            "path": "memories/cloudflare.md",
        },
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "type": "memory",
            "title": "Random Note",
            "body": "Bought a coffee maker. Brewed coffee on Tuesday.",
            "path": "memories/coffee.md",
        },
        {
            "id": "55555555-5555-5555-5555-555555555555",
            "type": "memory",
            "title": "Backup Locations",
            "body": "Daily backups go to S3. Weekly snapshots to Drive.",
            "path": "memories/backup.md",
        },
    ]
    for f in fixtures:
        metadata: dict[str, object] = {
            "id": f["id"],
            "type": f["type"],
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z",
            "source": "test",
            "title": f["title"],
        }
        if f["type"] == "skill":
            # Skills carry mandatory scope + verification per the
            # Frontmatter validator.
            metadata["scope"] = "universal"
            metadata["verification"] = "verify by hand"
        fm: Frontmatter = validate(metadata)
        memory = Memory(frontmatter=fm, body=f["body"], path=Path(f["path"]))
        vault.write(memory)
        index.upsert(memory)

    return Search(vault=vault, index=index, embedder=None)


def test_run_query_finds_match(fixture_search: Search) -> None:
    # Keyword-shaped query: every token in the query is also in the
    # fixture body. FTS5 defaults to AND-mode, so BM25-only setups
    # require token coverage; vec retrieval relaxes this in production
    # (and is what HyDE/W6 will further address). Unit tests
    # deliberately use keyword queries to exercise the harness without
    # depending on Ollama.
    query = EvalQuery(
        id="q1",
        class_="factual",
        query="Ari port 18789",
        expect=ExpectMatcher(body_contains=("18789",)),
    )
    result = run_query(fixture_search, query)
    assert result.found
    assert result.rank == 1
    assert result.reciprocal_rank == 1.0


def test_run_query_misses_when_answer_not_indexed(fixture_search: Search) -> None:
    query = EvalQuery(
        id="q1",
        class_="factual",
        query="Paris capital France Eiffel",
        expect=ExpectMatcher(body_contains=("Paris",)),
    )
    result = run_query(fixture_search, query)
    assert not result.found
    assert result.rank is None
    assert result.reciprocal_rank == 0.0


def test_run_query_top_k_caps_search(fixture_search: Search) -> None:
    query = EvalQuery(
        id="q1",
        class_="factual",
        query="coffee Tuesday brewed",
        expect=ExpectMatcher(body_contains=("coffee",)),
        top_k=1,
    )
    result = run_query(fixture_search, query)
    assert result.top_k == 1


def test_run_eval_aggregates_metrics(fixture_search: Search) -> None:
    queries = [
        EvalQuery(
            id="hit1",
            class_="factual",
            query="Ari port 18789",
            expect=ExpectMatcher(body_contains=("18789",)),
        ),
        EvalQuery(
            id="hit2",
            class_="procedural",
            query="tg-send Telegram Brad",
            expect=ExpectMatcher(body_contains=("tg-send",)),
        ),
        EvalQuery(
            id="miss",
            class_="historical",
            query="nonexistent xyz123 zzzqqq foo",
            expect=ExpectMatcher(body_contains=("nonexistent_token",)),
        ),
    ]
    report = run_eval(fixture_search, queries)
    assert report.total == 3
    assert report.found == 2
    assert 0.0 < report.mrr < 1.0
    assert report.recall_at_3 == pytest.approx(2 / 3)
    # Per-class
    assert "factual" in report.per_class
    assert "procedural" in report.per_class
    assert "historical" in report.per_class
    assert report.per_class["historical"]["found"] == 0.0
    assert report.per_class["factual"]["found"] == 1.0


def test_run_eval_empty_queries() -> None:
    # No vault touch; pure metric aggregation on zero queries.
    class _StubSearch:
        def search(self, *args: object, **kwargs: object) -> list[Result]:
            return []

    report = run_eval(_StubSearch(), [])  # type: ignore[arg-type]
    assert report.total == 0
    assert report.found == 0
    assert report.mrr == 0.0
    assert report.recall_at_3 == 0.0
    assert report.recall_at_10 == 0.0
    assert report.per_class == {}


# ─── report formatting + JSON ─────────────────────────────────────────


def test_format_report_includes_aggregate_and_per_class(fixture_search: Search) -> None:
    queries = [
        EvalQuery(
            id="hit1",
            class_="factual",
            query="Ari port 18789",
            expect=ExpectMatcher(body_contains=("18789",)),
        ),
        EvalQuery(
            id="miss",
            class_="historical",
            query="nonexistent xyz123 zzzqqq",
            expect=ExpectMatcher(body_contains=("nonexistent_token",)),
        ),
    ]
    report = run_eval(fixture_search, queries)
    text = format_report(report)
    assert "MEMSTEM EVAL HARNESS" in text
    assert "MRR:" in text
    assert "Recall@3:" in text
    assert "Recall@10:" in text
    assert "factual" in text
    assert "historical" in text
    assert "Failed queries" in text
    assert "miss" in text


def test_report_to_json_round_trip(fixture_search: Search) -> None:
    queries = [
        EvalQuery(
            id="q1",
            class_="factual",
            query="Ari port 18789",
            expect=ExpectMatcher(body_contains=("18789",)),
        ),
    ]
    report = run_eval(fixture_search, queries)
    payload = report_to_json(report)
    assert payload["total"] == 1
    assert "mrr" in payload
    assert "per_class" in payload
    assert "per_query" in payload
    assert payload["per_query"][0]["id"] == "q1"
    assert payload["per_query"][0]["class"] == "factual"
    assert "found" in payload["per_query"][0]


# ─── reciprocal_rank edge cases ───────────────────────────────────────


def test_query_result_reciprocal_rank_when_not_found() -> None:
    q = EvalQuery(
        id="q",
        class_="factual",
        query="x",
        expect=ExpectMatcher(body_contains=("y",)),
    )
    r = QueryResult(query=q, rank=None, top_k=10, elapsed_ms=1.0)
    assert r.reciprocal_rank == 0.0
    assert not r.found


def test_query_result_reciprocal_rank_at_various_ranks() -> None:
    q = EvalQuery(
        id="q",
        class_="factual",
        query="x",
        expect=ExpectMatcher(body_contains=("y",)),
    )
    assert QueryResult(query=q, rank=1, top_k=10, elapsed_ms=1.0).reciprocal_rank == 1.0
    assert QueryResult(query=q, rank=2, top_k=10, elapsed_ms=1.0).reciprocal_rank == 0.5
    assert QueryResult(query=q, rank=10, top_k=10, elapsed_ms=1.0).reciprocal_rank == 0.1
