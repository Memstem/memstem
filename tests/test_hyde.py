"""Tests for ``memstem.core.hyde`` — HyDE query expansion (ADR 0018)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memstem.core.hyde import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    MIN_QUERY_TOKENS,
    HydeExpander,
    NoOpExpander,
    OllamaExpander,
    OpenAIExpander,
    StubExpander,
    cache_lookup,
    cache_write,
    query_hash,
    should_expand,
)
from memstem.core.index import Index


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


# ─── query_hash ───────────────────────────────────────────────────


class TestQueryHash:
    def test_stable_for_same_input(self) -> None:
        assert query_hash("alpha") == query_hash("alpha")

    def test_differs_per_input(self) -> None:
        assert query_hash("alpha") != query_hash("beta")


# ─── should_expand gate ───────────────────────────────────────────


class TestShouldExpand:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            # Real questions — should expand.
            ("how do I send a Telegram message", True),
            ("what gateway port does Ari run on", True),
            ("memory consolidation strategy", True),
            # Too short — exact lookups, skip.
            ("ari", False),
            ("ari port", False),
            # Quoted strings — exact match intent.
            ('alpha "exact phrase" query', False),
            # Boolean operators.
            ("alpha AND beta", False),
            ("alpha OR beta", False),
            ("alpha NOT gamma", False),
            ("+alpha -beta", False),
            # Identifier shapes.
            ("11111111-1111-1111-1111-111111111111", False),
            ("a" * 32, False),
            ("/home/ubuntu/memstem/file.md", False),
            ("memstem/core/search.py", False),
            # Empty / whitespace.
            ("", False),
            ("   ", False),
        ],
    )
    def test_gate(self, query: str, expected: bool) -> None:
        assert should_expand(query) is expected

    def test_minimum_tokens_constant(self) -> None:
        assert MIN_QUERY_TOKENS >= 2

    def test_class_method_delegates_to_module_function(self) -> None:
        # HydeExpander.should_expand defaults to the module-level gate;
        # subclasses can override but the default must match.
        expander = NoOpExpander()
        assert expander.should_expand("how do I send a message") is True
        assert expander.should_expand("ari") is False


# ─── cache helpers ────────────────────────────────────────────────


class TestCache:
    def test_lookup_miss_returns_none(self, index: Index) -> None:
        assert cache_lookup(index.db, qhash="qh", judge="stub") is None

    def test_write_then_lookup_roundtrip(self, index: Index) -> None:
        cache_write(
            index.db,
            qhash="qh",
            judge="stub",
            hypothesis="this is a hypothesis passage",
        )
        got = cache_lookup(index.db, qhash="qh", judge="stub")
        assert got == "this is a hypothesis passage"

    def test_lookup_isolated_per_judge(self, index: Index) -> None:
        cache_write(index.db, qhash="qh", judge="stub", hypothesis="A")
        cache_write(index.db, qhash="qh", judge="ollama:qwen2.5:7b", hypothesis="B")
        assert cache_lookup(index.db, qhash="qh", judge="stub") == "A"
        assert cache_lookup(index.db, qhash="qh", judge="ollama:qwen2.5:7b") == "B"

    def test_lookup_isolated_per_query(self, index: Index) -> None:
        cache_write(index.db, qhash="qh1", judge="stub", hypothesis="first")
        cache_write(index.db, qhash="qh2", judge="stub", hypothesis="second")
        assert cache_lookup(index.db, qhash="qh1", judge="stub") == "first"
        assert cache_lookup(index.db, qhash="qh2", judge="stub") == "second"

    def test_write_is_idempotent(self, index: Index) -> None:
        for h in ("first", "second", "third"):
            cache_write(index.db, qhash="qh", judge="stub", hypothesis=h)
        assert cache_lookup(index.db, qhash="qh", judge="stub") == "third"


# ─── NoOpExpander ─────────────────────────────────────────────────


class TestNoOpExpander:
    def test_expand_returns_query_unchanged(self) -> None:
        expander = NoOpExpander()
        assert expander.expand("how do I send a message") == "how do I send a message"

    def test_expand_cached_skips_db(self, index: Index) -> None:
        # NoOp must NOT touch the cache — its output is constant.
        expander = NoOpExpander()
        result = expander.expand_cached("hello world", db=index.db)
        assert result == "hello world"
        rows = index.db.execute("SELECT COUNT(*) FROM hyde_cache").fetchone()[0]
        assert rows == 0


# ─── StubExpander ─────────────────────────────────────────────────


class TestStubExpander:
    def test_set_hypothesis_then_expand(self) -> None:
        expander = StubExpander()
        expander.set_hypothesis("how do I X", "to do X, run command Y")
        assert expander.expand("how do I X") == "to do X, run command Y"

    def test_unknown_query_returns_default(self) -> None:
        expander = StubExpander()
        # Empty default is falsy; callers fall through to original query.
        assert expander.expand("unknown query") == ""
        expander.set_default("default hypothesis")
        assert expander.expand("unknown query") == "default hypothesis"

    def test_expand_cached_writes_cache(self, index: Index) -> None:
        expander = StubExpander()
        expander.set_hypothesis("how do I X", "Run command Y to do X")
        result = expander.expand_cached("how do I X", db=index.db)
        assert result == "Run command Y to do X"
        rows = index.db.execute("SELECT COUNT(*) FROM hyde_cache").fetchone()[0]
        assert rows == 1

    def test_expand_cached_uses_cache_on_second_call(self, index: Index) -> None:
        expander = StubExpander()
        expander.set_hypothesis("how do I X", "first answer")
        first = expander.expand_cached("how do I X", db=index.db)
        # Change the stub's hypothesis. Cache should still win.
        expander.set_hypothesis("how do I X", "different answer")
        second = expander.expand_cached("how do I X", db=index.db)
        assert first == "first answer"
        assert second == "first answer", "cache hit should beat new stub config"

    def test_expand_cached_no_db_skips_cache(self) -> None:
        expander = StubExpander()
        expander.set_hypothesis("q", "h")
        assert expander.expand_cached("q", db=None) == "h"

    def test_empty_hypothesis_not_cached(self, index: Index) -> None:
        # An empty hypothesis means "expander failed"; we shouldn't
        # cache failure (the cache assumes its rows are valid hits).
        expander = StubExpander()
        # Default is empty — query returns "" → not cached.
        result = expander.expand_cached("unknown", db=index.db)
        assert result == ""
        rows = index.db.execute("SELECT COUNT(*) FROM hyde_cache").fetchone()[0]
        assert rows == 0


# ─── OllamaExpander (mocked client) ───────────────────────────────


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


class TestOllamaExpander:
    def test_default_name_includes_model(self) -> None:
        expander = OllamaExpander(client=_MockHttpClient(_MockHttpResponse(body={"response": "x"})))
        assert expander.name == f"ollama:{DEFAULT_OLLAMA_MODEL}"

    def test_expand_returns_passage(self) -> None:
        passage = "To send a Telegram message, run bash ~/scripts/tg-send 'msg'."
        client = _MockHttpClient(_MockHttpResponse(body={"response": passage}))
        expander = OllamaExpander(client=client)
        result = expander.expand("how do I send a Telegram message")
        assert result == passage
        assert len(client.calls) == 1
        url, body = client.calls[0]
        assert url == "/api/generate"
        assert body["model"] == DEFAULT_OLLAMA_MODEL
        assert body["stream"] is False
        # Prompt should mention the query.
        assert "how do I send a Telegram message" in body["prompt"]

    def test_expand_strips_code_fences(self) -> None:
        fenced = "```\nA passage about the topic.\n```"
        client = _MockHttpClient(_MockHttpResponse(body={"response": fenced}))
        expander = OllamaExpander(client=client)
        assert expander.expand("how do I send a Telegram message") == "A passage about the topic."

    def test_expand_returns_empty_on_http_failure(self) -> None:
        client = _MockHttpClient(RuntimeError("boom"))
        expander = OllamaExpander(client=client)
        # Empty hypothesis signals to the caller to fall back to
        # the original query.
        assert expander.expand("query") == ""

    def test_expand_cached_falls_back_silently_on_failure(self, index: Index) -> None:
        client = _MockHttpClient(RuntimeError("boom"))
        expander = OllamaExpander(client=client)
        # expand_cached must NOT cache empty hypotheses — caching them
        # would lock in failure for every subsequent call until a
        # manual cache clear.
        result = expander.expand_cached("how do I X", db=index.db)
        assert result == ""
        rows = index.db.execute("SELECT COUNT(*) FROM hyde_cache").fetchone()[0]
        assert rows == 0


# ─── ABC contract ─────────────────────────────────────────────────


def test_expander_is_abstract() -> None:
    """Direct instantiation of the ABC should fail."""
    with pytest.raises(TypeError):
        HydeExpander()  # type: ignore[abstract]


# ─── Failure isolation ────────────────────────────────────────────


class _FailingExpander(HydeExpander):
    name = "failing"

    def expand(self, query: str) -> str:
        raise RuntimeError("intentional failure")


def test_expand_cached_swallows_exceptions(index: Index) -> None:
    """A failing expander must not crash search."""
    expander = _FailingExpander()
    result = expander.expand_cached("how do I X", db=index.db)
    assert result == ""


# ─── OpenAIExpander (mocked client) ───────────────────────────────


def _openai_response(content: str) -> _MockHttpResponse:
    """Build a mock OpenAI chat-completions response body."""
    return _MockHttpResponse(
        body={"choices": [{"message": {"role": "assistant", "content": content}, "index": 0}]}
    )


class TestOpenAIExpander:
    def test_default_name_includes_model(self) -> None:
        expander = OpenAIExpander(client=_MockHttpClient(_openai_response("x")))
        assert expander.name == f"openai:{DEFAULT_OPENAI_MODEL}"

    def test_custom_model_in_name(self) -> None:
        expander = OpenAIExpander(model="gpt-4o", client=_MockHttpClient(_openai_response("x")))
        assert expander.name == "openai:gpt-4o"

    def test_expand_returns_passage(self) -> None:
        passage = "To send a Telegram message, run bash ~/scripts/tg-send 'msg'."
        client = _MockHttpClient(_openai_response(passage))
        expander = OpenAIExpander(client=client)
        result = expander.expand("how do I send a Telegram message")
        assert result == passage
        assert len(client.calls) == 1
        url, body = client.calls[0]
        assert url == "/chat/completions"
        assert body["model"] == DEFAULT_OPENAI_MODEL
        assert body["messages"][0]["role"] == "user"
        assert "how do I send a Telegram message" in body["messages"][0]["content"]
        # Passage shape: mild temperature, generous max tokens.
        assert body["temperature"] == 0.3
        assert body["max_completion_tokens"] == 200

    def test_expand_strips_code_fences(self) -> None:
        client = _MockHttpClient(_openai_response("```\nA passage about X.\n```"))
        expander = OpenAIExpander(client=client)
        assert expander.expand("how do I send a Telegram message") == "A passage about X."

    def test_expand_returns_empty_on_http_failure(self) -> None:
        client = _MockHttpClient(RuntimeError("boom"))
        expander = OpenAIExpander(client=client)
        assert expander.expand("query") == ""

    def test_expand_handles_empty_choices(self) -> None:
        client = _MockHttpClient(_MockHttpResponse(body={"choices": []}))
        expander = OpenAIExpander(client=client)
        # Empty choices → empty content → fall through.
        assert expander.expand("how do I X") == ""

    def test_cache_isolated_from_ollama_judge(self, index: Index) -> None:
        """Same query, different judges → independent cache rows."""
        oa_client = _MockHttpClient(_openai_response("OpenAI hypothesis"))
        oa = OpenAIExpander(client=oa_client)
        oa_result = oa.expand_cached("how do I send a Telegram message", db=index.db)

        ol_client = _MockHttpClient(_MockHttpResponse(body={"response": "Ollama hypothesis"}))
        ol = OllamaExpander(client=ol_client)
        ol_result = ol.expand_cached("how do I send a Telegram message", db=index.db)

        assert oa_result == "OpenAI hypothesis"
        assert ol_result == "Ollama hypothesis"
        # Both judges hit the network (no cross-judge cache hit).
        assert len(oa_client.calls) == 1
        assert len(ol_client.calls) == 1
        # Two distinct cache rows.
        rows = index.db.execute("SELECT COUNT(*) FROM hyde_cache").fetchone()[0]
        assert rows == 2
