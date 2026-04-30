"""Tests for ``memstem.core.rerank`` — cross-encoder rerank (ADR 0017)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memstem.core.frontmatter import Frontmatter, validate
from memstem.core.index import Index
from memstem.core.rerank import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_RERANK_TOP_N,
    MAX_RERANK_BODY_CHARS,
    NoOpReranker,
    OllamaReranker,
    OpenAIReranker,
    RerankCandidate,
    Reranker,
    StubReranker,
    _format_body_for_prompt,
    _parse_score,
    cache_lookup,
    cache_write,
    query_hash,
)
from memstem.core.storage import Memory


def _memory(memory_id: str, body: str, title: str = "untitled") -> Memory:
    fm: Frontmatter = validate(
        {
            "id": memory_id,
            "type": "memory",
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z",
            "source": "test",
            "title": title,
        }
    )
    return Memory(frontmatter=fm, body=body, path=Path(f"memories/{memory_id}.md"))


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


# ─── RerankCandidate ──────────────────────────────────────────────


class TestRerankCandidate:
    def test_from_memory_captures_payload(self) -> None:
        memory = _memory(
            memory_id="11111111-1111-1111-1111-111111111111",
            body="hello world",
            title="greeting",
        )
        candidate = RerankCandidate.from_memory(memory)
        assert candidate.memory_id == "11111111-1111-1111-1111-111111111111"
        assert candidate.title == "greeting"
        assert candidate.body == "hello world"
        # body_hash must equal SHA-256 of UTF-8-encoded body.
        assert candidate.body_hash == hashlib.sha256(b"hello world").hexdigest()

    def test_from_memory_handles_missing_title(self) -> None:
        fm = validate(
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "type": "memory",
                "created": "2026-01-01T00:00:00Z",
                "updated": "2026-01-01T00:00:00Z",
                "source": "test",
            }
        )
        memory = Memory(frontmatter=fm, body="x", path=Path("memories/m.md"))
        candidate = RerankCandidate.from_memory(memory)
        assert candidate.title == ""

    def test_body_hash_changes_with_body(self) -> None:
        memory_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        a = RerankCandidate.from_memory(_memory(memory_id, "first"))
        b = RerankCandidate.from_memory(_memory(memory_id, "second"))
        assert a.body_hash != b.body_hash


# ─── query_hash ───────────────────────────────────────────────────


class TestQueryHash:
    def test_stable_for_same_input(self) -> None:
        assert query_hash("alpha") == query_hash("alpha")

    def test_differs_per_input(self) -> None:
        assert query_hash("alpha") != query_hash("beta")

    def test_unicode_safe(self) -> None:
        # Unicode bodies should hash without exploding.
        assert query_hash("café") == hashlib.sha256("café".encode()).hexdigest()


# ─── cache helpers ────────────────────────────────────────────────


class TestCache:
    def test_lookup_miss_returns_none(self, index: Index) -> None:
        result = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="m1",
            body_hash="bh",
            judge="stub",
        )
        assert result is None

    def test_write_then_lookup_roundtrip(self, index: Index) -> None:
        # cache_write requires the memory_id to exist (FK), so insert one.
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p1.md', '2026-01-01', '2026-01-01')""",
            ("11111111-1111-1111-1111-111111111111",),
        )
        cache_write(
            index.db,
            qhash="qh",
            memory_id="11111111-1111-1111-1111-111111111111",
            body_hash="bh",
            judge="stub",
            score=0.42,
        )
        got = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="11111111-1111-1111-1111-111111111111",
            body_hash="bh",
            judge="stub",
        )
        assert got == pytest.approx(0.42)

    def test_lookup_isolated_per_judge(self, index: Index) -> None:
        # Different judges with otherwise-identical keys must not collide.
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p2.md', '2026-01-01', '2026-01-01')""",
            ("22222222-2222-2222-2222-222222222222",),
        )
        cache_write(
            index.db,
            qhash="qh",
            memory_id="22222222-2222-2222-2222-222222222222",
            body_hash="bh",
            judge="stub",
            score=0.4,
        )
        cache_write(
            index.db,
            qhash="qh",
            memory_id="22222222-2222-2222-2222-222222222222",
            body_hash="bh",
            judge="ollama:qwen2.5:7b",
            score=0.7,
        )
        a = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="22222222-2222-2222-2222-222222222222",
            body_hash="bh",
            judge="stub",
        )
        b = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="22222222-2222-2222-2222-222222222222",
            body_hash="bh",
            judge="ollama:qwen2.5:7b",
        )
        assert a == pytest.approx(0.4)
        assert b == pytest.approx(0.7)

    def test_lookup_misses_on_body_hash_change(self, index: Index) -> None:
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p3.md', '2026-01-01', '2026-01-01')""",
            ("33333333-3333-3333-3333-333333333333",),
        )
        cache_write(
            index.db,
            qhash="qh",
            memory_id="33333333-3333-3333-3333-333333333333",
            body_hash="old",
            judge="stub",
            score=0.5,
        )
        # New body hash → cache miss, even though every other key matches.
        got = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="33333333-3333-3333-3333-333333333333",
            body_hash="new",
            judge="stub",
        )
        assert got is None

    def test_write_is_idempotent(self, index: Index) -> None:
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p4.md', '2026-01-01', '2026-01-01')""",
            ("44444444-4444-4444-4444-444444444444",),
        )
        for value in (0.1, 0.5, 0.9):
            cache_write(
                index.db,
                qhash="qh",
                memory_id="44444444-4444-4444-4444-444444444444",
                body_hash="bh",
                judge="stub",
                score=value,
            )
        got = cache_lookup(
            index.db,
            qhash="qh",
            memory_id="44444444-4444-4444-4444-444444444444",
            body_hash="bh",
            judge="stub",
        )
        # Last write wins.
        assert got == pytest.approx(0.9)


# ─── NoOpReranker ─────────────────────────────────────────────────


class TestNoOpReranker:
    def test_score_returns_one(self) -> None:
        reranker = NoOpReranker()
        candidate = RerankCandidate.from_memory(
            _memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "anything")
        )
        assert reranker.score("query", candidate) == 1.0

    def test_score_candidates_skips_cache(self, index: Index) -> None:
        # NoOp must NOT touch the cache — its output is constant.
        reranker = NoOpReranker()
        candidates = [
            RerankCandidate.from_memory(_memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "x")),
            RerankCandidate.from_memory(_memory("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "y")),
        ]
        scores = reranker.score_candidates("q", candidates, db=index.db)
        assert scores == [1.0, 1.0]
        # Cache should be untouched.
        rows = index.db.execute("SELECT COUNT(*) FROM rerank_cache").fetchone()[0]
        assert rows == 0


# ─── StubReranker ─────────────────────────────────────────────────


class TestStubReranker:
    def test_set_score_then_lookup(self) -> None:
        reranker = StubReranker()
        memory_id = "11111111-1111-1111-1111-111111111111"
        candidate = RerankCandidate.from_memory(_memory(memory_id, "x"))
        reranker.set_score("hello", memory_id, 0.8)
        assert reranker.score("hello", candidate) == pytest.approx(0.8)

    def test_unknown_pair_returns_default(self) -> None:
        reranker = StubReranker()
        candidate = RerankCandidate.from_memory(
            _memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "x")
        )
        assert reranker.score("hello", candidate) == pytest.approx(0.5)
        reranker.set_default(0.1)
        assert reranker.score("hello", candidate) == pytest.approx(0.1)

    def test_score_candidates_caches_results(self, index: Index) -> None:
        memory_id = "55555555-5555-5555-5555-555555555555"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'hello world', 'p5.md', '2026-01-01', '2026-01-01')""",
            (memory_id,),
        )
        memory = _memory(memory_id, "hello world")
        reranker = StubReranker()
        reranker.set_score("query", memory_id, 0.77)
        candidates = [RerankCandidate.from_memory(memory)]
        scores = reranker.score_candidates("query", candidates, db=index.db)
        assert scores == [pytest.approx(0.77)]
        # Cache row should exist.
        row_count = index.db.execute("SELECT COUNT(*) FROM rerank_cache").fetchone()[0]
        assert row_count == 1

    def test_score_candidates_uses_cache_on_second_call(self, index: Index) -> None:
        memory_id = "66666666-6666-6666-6666-666666666666"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p6.md', '2026-01-01', '2026-01-01')""",
            (memory_id,),
        )
        memory = _memory(memory_id, "b")
        reranker = StubReranker()
        reranker.set_score("query", memory_id, 0.4)
        candidates = [RerankCandidate.from_memory(memory)]
        first = reranker.score_candidates("query", candidates, db=index.db)
        # Now change the stub's stored score. Cache hit should ignore it.
        reranker.set_score("query", memory_id, 0.99)
        second = reranker.score_candidates("query", candidates, db=index.db)
        assert first == [pytest.approx(0.4)]
        assert second == [pytest.approx(0.4)], "cache hit should win over re-scoring"

    def test_score_candidates_no_db_skips_cache(self) -> None:
        memory_id = "77777777-7777-7777-7777-777777777777"
        memory = _memory(memory_id, "b")
        reranker = StubReranker()
        reranker.set_score("query", memory_id, 0.4)
        scores = reranker.score_candidates(
            "query",
            [RerankCandidate.from_memory(memory)],
            db=None,
        )
        assert scores == [pytest.approx(0.4)]


# ─── _format_body_for_prompt ──────────────────────────────────────


class TestFormatBodyForPrompt:
    def test_short_body_unchanged(self) -> None:
        short = "a short document body"
        assert _format_body_for_prompt(short) == short

    def test_body_at_cap_unchanged(self) -> None:
        body = "x" * MAX_RERANK_BODY_CHARS
        assert _format_body_for_prompt(body) == body

    def test_oversize_body_truncated_with_marker(self) -> None:
        body = "x" * (MAX_RERANK_BODY_CHARS + 1000)
        out = _format_body_for_prompt(body)
        assert len(out) < len(body)
        # Head must be the original first MAX_RERANK_BODY_CHARS chars.
        assert out.startswith("x" * MAX_RERANK_BODY_CHARS)
        # Marker tells the LLM how much was elided.
        assert "1,000 more chars" in out
        assert "document continues" in out

    def test_huge_body_truncates_to_bounded_size(self) -> None:
        # The 1.7 MB Brad-vault case: must produce something well under
        # any provider's context window.
        body = "x" * 1_700_000
        out = _format_body_for_prompt(body)
        # Output is the head slice + marker; bounded by MAX + ~80 chars.
        assert len(out) <= MAX_RERANK_BODY_CHARS + 200


# ─── _parse_score ─────────────────────────────────────────────────


class TestParseScore:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("85", 0.85),
            ("0", 0.0),
            ("100", 1.0),
            ("Score: 70", 0.70),
            ("70/100", 0.70),
            ("The relevance is 42 because of X", 0.42),
            ('{"score": 60}', 0.60),
            ('{"score": "60"}', 0.60),
            ("", 0.0),
            ("no number here", 0.0),
            ("150", 1.0),  # clamp upper
            ("-30", 0.0),  # negatives never make sense for relevance
        ],
    )
    def test_parses_or_clamps(self, text: str, expected: float) -> None:
        assert _parse_score(text) == pytest.approx(expected)


# ─── OllamaReranker (mocked client) ───────────────────────────────


class _MockHttpResponse:
    def __init__(self, *, body: dict[str, Any], status: int = 200) -> None:
        self._body = body
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self) -> dict[str, Any]:
        return self._body


class _MockHttpClient:
    def __init__(self, response: _MockHttpResponse | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, *, json: dict[str, Any]) -> Any:
        self.calls.append((url, json))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestOllamaReranker:
    def test_default_name_includes_model(self) -> None:
        reranker = OllamaReranker(client=_MockHttpClient(_MockHttpResponse(body={"response": "0"})))
        assert reranker.name == f"ollama:{DEFAULT_OLLAMA_MODEL}"

    def test_score_parses_response(self) -> None:
        client = _MockHttpClient(_MockHttpResponse(body={"response": "85"}))
        reranker = OllamaReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("11111111-1111-1111-1111-111111111111", "doc body")
        )
        score = reranker.score("query about the doc", candidate)
        assert score == pytest.approx(0.85)
        # Must have hit /api/generate exactly once.
        assert len(client.calls) == 1
        url, body = client.calls[0]
        assert url == "/api/generate"
        assert body["model"] == DEFAULT_OLLAMA_MODEL
        assert body["stream"] is False
        # Prompt should mention the query and document body.
        assert "query about the doc" in body["prompt"]
        assert "doc body" in body["prompt"]

    def test_score_returns_zero_on_http_failure(self) -> None:
        client = _MockHttpClient(RuntimeError("boom"))
        reranker = OllamaReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("22222222-2222-2222-2222-222222222222", "b")
        )
        # Score must NOT propagate the exception — fall through to 0.0.
        assert reranker.score("q", candidate) == 0.0

    def test_score_truncates_oversize_body_in_prompt(self) -> None:
        """OllamaReranker must also truncate — qwen2.5:7b has a 32k context."""
        client = _MockHttpClient(_MockHttpResponse(body={"response": "70"}))
        reranker = OllamaReranker(client=client)
        big_body = "alpha topic body " + ("filler text " * 8000)
        assert len(big_body) > MAX_RERANK_BODY_CHARS
        candidate = RerankCandidate.from_memory(
            _memory("88888888-8888-8888-8888-888888888888", big_body)
        )
        reranker.score("query", candidate)
        _, body = client.calls[0]
        sent_prompt = body["prompt"]
        assert len(sent_prompt) <= MAX_RERANK_BODY_CHARS + 2500

    def test_score_handles_malformed_response(self) -> None:
        client = _MockHttpClient(_MockHttpResponse(body={"response": "I have opinions"}))
        reranker = OllamaReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("33333333-3333-3333-3333-333333333333", "b")
        )
        # No integer in response → 0.0
        assert reranker.score("q", candidate) == 0.0

    def test_score_clamps_out_of_range(self) -> None:
        client = _MockHttpClient(_MockHttpResponse(body={"response": "999"}))
        reranker = OllamaReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("44444444-4444-4444-4444-444444444444", "b")
        )
        assert reranker.score("q", candidate) == 1.0

    def test_score_candidates_writes_cache(self, index: Index) -> None:
        # Mock a real Ollama cycle through score_candidates: cache miss
        # → score() called → cache row appears.
        memory_id = "88888888-8888-8888-8888-888888888888"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'p8.md', '2026-01-01', '2026-01-01')""",
            (memory_id,),
        )
        client = _MockHttpClient(_MockHttpResponse(body={"response": "70"}))
        reranker = OllamaReranker(client=client)
        candidate = RerankCandidate.from_memory(_memory(memory_id, "b"))
        scores = reranker.score_candidates("q", [candidate], db=index.db)
        assert scores == [pytest.approx(0.7)]
        # One LLM call.
        assert len(client.calls) == 1
        # Second call is a cache hit — no additional LLM round trip.
        scores_again = reranker.score_candidates("q", [candidate], db=index.db)
        assert scores_again == [pytest.approx(0.7)]
        assert len(client.calls) == 1, "cache hit should not invoke the model"


# ─── score_candidates failure isolation ───────────────────────────


class _FailingReranker(Reranker):
    name = "failing"

    def score(self, query: str, candidate: RerankCandidate) -> float:
        raise RuntimeError("intentional failure")


def test_score_candidates_swallows_per_call_failures(index: Index) -> None:
    """A failing reranker must not crash the search path."""
    memory_id = "99999999-9999-9999-9999-999999999999"
    index.db.execute(
        """INSERT INTO memories(id, type, source, title, body, path, created, updated)
           VALUES (?, 'memory', 'test', 't', 'b', 'p9.md', '2026-01-01', '2026-01-01')""",
        (memory_id,),
    )
    reranker = _FailingReranker()
    scores = reranker.score_candidates(
        "q",
        [RerankCandidate.from_memory(_memory(memory_id, "b"))],
        db=index.db,
    )
    # Failure → 0.0 fallback, not an exception.
    assert scores == [0.0]


def test_default_constants_exposed() -> None:
    assert DEFAULT_RERANK_TOP_N >= 1
    assert DEFAULT_OLLAMA_MODEL
    assert DEFAULT_OPENAI_MODEL


def test_reranker_is_abstract() -> None:
    """Direct instantiation of the ABC should fail."""
    with pytest.raises(TypeError):
        Reranker()  # type: ignore[abstract]


# ─── OpenAIReranker (mocked client) ───────────────────────────────


def _openai_response(content: str) -> _MockHttpResponse:
    """Build a mock OpenAI chat-completions response body."""
    return _MockHttpResponse(
        body={"choices": [{"message": {"role": "assistant", "content": content}, "index": 0}]}
    )


class TestOpenAIReranker:
    def test_default_name_includes_model(self) -> None:
        reranker = OpenAIReranker(client=_MockHttpClient(_openai_response("0")))
        assert reranker.name == f"openai:{DEFAULT_OPENAI_MODEL}"

    def test_custom_model_in_name(self) -> None:
        reranker = OpenAIReranker(model="gpt-4o", client=_MockHttpClient(_openai_response("0")))
        assert reranker.name == "openai:gpt-4o"

    def test_score_parses_response(self) -> None:
        client = _MockHttpClient(_openai_response("85"))
        reranker = OpenAIReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("11111111-1111-1111-1111-111111111111", "doc body")
        )
        score = reranker.score("query about the doc", candidate)
        assert score == pytest.approx(0.85)
        # Must have hit /chat/completions exactly once.
        assert len(client.calls) == 1
        url, body = client.calls[0]
        assert url == "/chat/completions"
        assert body["model"] == DEFAULT_OPENAI_MODEL
        assert body["messages"][0]["role"] == "user"
        # Prompt should mention the query and document body.
        assert "query about the doc" in body["messages"][0]["content"]
        assert "doc body" in body["messages"][0]["content"]
        # Low temperature for stability.
        assert body["temperature"] == 0.0

    def test_score_truncates_oversize_body_in_prompt(self) -> None:
        """A 1.7MB-style body must be truncated before reaching the API."""
        client = _MockHttpClient(_openai_response("70"))
        reranker = OpenAIReranker(client=client)
        # Build a candidate with a 100k body — well over the truncation cap.
        big_body = "alpha topic body " + ("filler text " * 8000)
        assert len(big_body) > MAX_RERANK_BODY_CHARS
        candidate = RerankCandidate.from_memory(
            _memory("99999999-9999-9999-9999-999999999999", big_body)
        )
        reranker.score("query", candidate)
        # The prompt sent to OpenAI must be bounded by the truncation cap.
        _, body = client.calls[0]
        sent_prompt = body["messages"][0]["content"]
        # Prompt template (~1.5k chars rubric) + truncated body should
        # fit well under any provider's context window.
        assert len(sent_prompt) <= MAX_RERANK_BODY_CHARS + 2500
        # And the candidate's body_hash on disk still reflects the FULL
        # body — cache invalidation works correctly when content changes
        # even if the truncated slice happens to be identical.
        assert candidate.body == big_body

    def test_score_returns_zero_on_http_failure(self) -> None:
        client = _MockHttpClient(RuntimeError("boom"))
        reranker = OpenAIReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("22222222-2222-2222-2222-222222222222", "b")
        )
        assert reranker.score("q", candidate) == 0.0

    def test_score_handles_empty_choices(self) -> None:
        client = _MockHttpClient(_MockHttpResponse(body={"choices": []}))
        reranker = OpenAIReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("33333333-3333-3333-3333-333333333333", "b")
        )
        # Empty choices → empty content → 0.0 from _parse_score.
        assert reranker.score("q", candidate) == 0.0

    def test_score_clamps_out_of_range(self) -> None:
        client = _MockHttpClient(_openai_response("999"))
        reranker = OpenAIReranker(client=client)
        candidate = RerankCandidate.from_memory(
            _memory("44444444-4444-4444-4444-444444444444", "b")
        )
        assert reranker.score("q", candidate) == 1.0

    def test_score_candidates_writes_cache(self, index: Index) -> None:
        memory_id = "55555555-5555-5555-5555-555555555555"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'po1.md', '2026-01-01', '2026-01-01')""",
            (memory_id,),
        )
        client = _MockHttpClient(_openai_response("70"))
        reranker = OpenAIReranker(client=client)
        candidate = RerankCandidate.from_memory(_memory(memory_id, "b"))
        scores = reranker.score_candidates("q", [candidate], db=index.db)
        assert scores == [pytest.approx(0.7)]
        assert len(client.calls) == 1
        # Second call hits cache, no extra HTTP.
        scores_again = reranker.score_candidates("q", [candidate], db=index.db)
        assert scores_again == [pytest.approx(0.7)]
        assert len(client.calls) == 1

    def test_cache_isolated_from_ollama_judge(self, index: Index) -> None:
        """Same query+memory+body, different judges → independent cache rows."""
        memory_id = "66666666-6666-6666-6666-666666666666"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'po2.md', '2026-01-01', '2026-01-01')""",
            (memory_id,),
        )
        candidate = RerankCandidate.from_memory(_memory(memory_id, "b"))
        # Score with OpenAI judge.
        oa_client = _MockHttpClient(_openai_response("80"))
        oa_reranker = OpenAIReranker(client=oa_client)
        oa_scores = oa_reranker.score_candidates("q", [candidate], db=index.db)
        # Score with Ollama judge — must NOT use the OpenAI cache row.
        ol_client = _MockHttpClient(_MockHttpResponse(body={"response": "30"}))
        ol_reranker = OllamaReranker(client=ol_client)
        ol_scores = ol_reranker.score_candidates("q", [candidate], db=index.db)
        assert oa_scores == [pytest.approx(0.8)]
        assert ol_scores == [pytest.approx(0.3)]
        # Both judges hit the network (no cross-judge cache hit).
        assert len(oa_client.calls) == 1
        assert len(ol_client.calls) == 1
        # Two distinct cache rows now.
        rows = index.db.execute("SELECT COUNT(*) FROM rerank_cache").fetchone()[0]
        assert rows == 2
