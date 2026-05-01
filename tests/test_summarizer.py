"""Tests for the generic LLM summarizer abstraction (ADRs 0020 + 0021)."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest

from memstem.core import summarizer as smod


@pytest.fixture
def cache_db(tmp_path: Any) -> sqlite3.Connection:
    """In-memory cache db with the v11 schema applied.

    Tests don't need the full Index for the summarizer cache — just
    the one table the summarizer reads/writes.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS summarizer_cache (
            content_hash TEXT NOT NULL,
            summarizer TEXT NOT NULL,
            output TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY (content_hash, summarizer)
        );
        CREATE INDEX IF NOT EXISTS idx_summarizer_cache_ts ON summarizer_cache(ts);
        """
    )
    return db


# ─── content_hash + cache helpers ─────────────────────────────────


def test_content_hash_is_stable_for_identical_prompts() -> None:
    a = smod.content_hash("hello world")
    b = smod.content_hash("hello world")
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_content_hash_changes_with_prompt() -> None:
    assert smod.content_hash("a") != smod.content_hash("b")
    # Whitespace matters — the cache should invalidate on template
    # tweaks even if the content is "the same".
    assert smod.content_hash("hello") != smod.content_hash(" hello")


def test_cache_lookup_miss_returns_none(cache_db: sqlite3.Connection) -> None:
    assert smod.cache_lookup(cache_db, chash="nope", summarizer="x") is None


def test_cache_write_then_lookup_round_trips(cache_db: sqlite3.Connection) -> None:
    smod.cache_write(cache_db, chash="abc", summarizer="openai:gpt-5.4-mini", output="hi")
    got = smod.cache_lookup(cache_db, chash="abc", summarizer="openai:gpt-5.4-mini")
    assert got == "hi"


def test_cache_write_is_upsert(cache_db: sqlite3.Connection) -> None:
    smod.cache_write(cache_db, chash="abc", summarizer="x", output="first")
    smod.cache_write(cache_db, chash="abc", summarizer="x", output="second")
    assert smod.cache_lookup(cache_db, chash="abc", summarizer="x") == "second"
    # And only one row.
    rows = cache_db.execute("SELECT COUNT(*) FROM summarizer_cache").fetchone()[0]
    assert rows == 1


def test_cache_lookup_swallows_sqlite_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closed connection raises ProgrammingError on .execute(); cache
    # should treat it as miss, not propagate.
    db = sqlite3.connect(":memory:")
    db.close()
    assert smod.cache_lookup(db, chash="x", summarizer="y") is None


def test_cache_write_swallows_sqlite_errors() -> None:
    db = sqlite3.connect(":memory:")
    db.close()
    # Should not raise.
    smod.cache_write(db, chash="x", summarizer="y", output="z")


# ─── NoOpSummarizer ───────────────────────────────────────────────


def test_noop_returns_empty_string() -> None:
    s = smod.NoOpSummarizer()
    assert s.generate("anything") == ""
    assert s.name == "noop"


def test_noop_generate_cached_skips_db(cache_db: sqlite3.Connection) -> None:
    s = smod.NoOpSummarizer()
    out = s.generate_cached("a prompt", db=cache_db)
    assert out == ""
    # Did NOT write to the cache.
    rows = cache_db.execute("SELECT COUNT(*) FROM summarizer_cache").fetchone()[0]
    assert rows == 0


# ─── StubSummarizer ───────────────────────────────────────────────


def test_stub_returns_default_when_unregistered() -> None:
    s = smod.StubSummarizer()
    assert s.generate("anything") == ""
    s.set_default("hello")
    assert s.generate("still anything") == "hello"


def test_stub_returns_registered_output() -> None:
    s = smod.StubSummarizer()
    s.set_output("specific prompt", "specific output")
    s.set_default("default")
    assert s.generate("specific prompt") == "specific output"
    assert s.generate("other") == "default"


def test_stub_generate_cached_writes_through(cache_db: sqlite3.Connection) -> None:
    s = smod.StubSummarizer()
    s.set_output("p", "o")
    out = s.generate_cached("p", db=cache_db)
    assert out == "o"
    # Cache should have one row keyed on ("stub", content_hash("p")).
    chash = smod.content_hash("p")
    cached = smod.cache_lookup(cache_db, chash=chash, summarizer="stub")
    assert cached == "o"


def test_stub_generate_cached_returns_cached_on_repeat(cache_db: sqlite3.Connection) -> None:
    s = smod.StubSummarizer()
    s.set_output("p", "first")
    s.generate_cached("p", db=cache_db)
    # Now flip the stub's mapping; the cache should still serve "first".
    s.set_output("p", "second")
    assert s.generate_cached("p", db=cache_db) == "first"


def test_stub_skips_cache_when_db_is_none() -> None:
    s = smod.StubSummarizer()
    s.set_output("p", "o")
    # No db, no cache writes — exercise the bypass path.
    assert s.generate_cached("p", db=None) == "o"


# ─── Generic Summarizer base behavior ─────────────────────────────


class _RaisingSummarizer(smod.Summarizer):
    """Subclass whose generate() always raises — exercises the safety net."""

    name = "raising"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("model is on fire")


def test_generate_cached_swallows_subclass_exceptions(cache_db: sqlite3.Connection) -> None:
    s = _RaisingSummarizer()
    out = s.generate_cached("anything", db=cache_db)
    assert out == ""
    # And we don't poison the cache with empty rows.
    rows = cache_db.execute("SELECT COUNT(*) FROM summarizer_cache").fetchone()[0]
    assert rows == 0


def test_generate_cached_skips_writing_empty_outputs(cache_db: sqlite3.Connection) -> None:
    """A summarizer that returns "" should not pollute the cache.

    Failure cases (empty output) re-try on the next run rather than
    permanently caching failure.
    """

    class _Empty(smod.Summarizer):
        name = "empty"

        def generate(self, prompt: str) -> str:
            return ""

    s = _Empty()
    s.generate_cached("p", db=cache_db)
    rows = cache_db.execute("SELECT COUNT(*) FROM summarizer_cache").fetchone()[0]
    assert rows == 0


def test_generate_cached_handles_non_string_returns(cache_db: sqlite3.Connection) -> None:
    """Defensive: a misbehaving subclass returning non-str should not crash."""

    class _Bogus(smod.Summarizer):
        name = "bogus"

        def generate(self, prompt: str) -> str:
            return 42  # type: ignore[return-value]

    s = _Bogus()
    out = s.generate_cached("p", db=cache_db)
    assert out == ""


# ─── _strip_fences ────────────────────────────────────────────────


def test_strip_fences_passes_through_unfenced_text() -> None:
    assert smod._strip_fences("plain prose") == "plain prose"
    assert smod._strip_fences("  spaced  ") == "spaced"


def test_strip_fences_strips_triple_backtick_blocks() -> None:
    fenced = "```markdown\n## Summary\n\nbody\n```"
    assert smod._strip_fences(fenced) == "## Summary\n\nbody"


def test_strip_fences_strips_bare_fences() -> None:
    fenced = "```\n## Summary\n\nbody\n```"
    assert smod._strip_fences(fenced) == "## Summary\n\nbody"


def test_strip_fences_leaves_partial_fences_alone() -> None:
    """A leading-only or trailing-only fence isn't treated as a wrapper."""
    only_leading = "```\n## Summary\nbody"
    assert smod._strip_fences(only_leading) == only_leading.strip()


# ─── OllamaSummarizer ─────────────────────────────────────────────


class _MockResponse:
    """Minimal httpx.Response stand-in for the mock client."""

    def __init__(self, json_body: dict[str, Any], status_code: int = 200) -> None:
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._json


class _MockClient:
    """Records the last POST and returns a canned response.

    Matches the `client.post(path, json=...)` shape both
    summarizers use.
    """

    def __init__(self, response: _MockResponse) -> None:
        self.response = response
        self.last_path: str | None = None
        self.last_json: dict[str, Any] | None = None

    def post(self, path: str, *, json: dict[str, Any]) -> _MockResponse:
        self.last_path = path
        self.last_json = json
        return self.response


def test_ollama_summarizer_name_includes_model() -> None:
    s = smod.OllamaSummarizer(model="qwen2.5:7b")
    assert s.name == "ollama:qwen2.5:7b"


def test_ollama_summarizer_calls_generate_endpoint() -> None:
    response = _MockResponse({"response": "summary text"})
    client = _MockClient(response)
    s = smod.OllamaSummarizer(client=client)

    out = s.generate("a prompt")

    assert out == "summary text"
    assert client.last_path == "/api/generate"
    body = client.last_json
    assert body is not None
    assert body["model"] == smod.DEFAULT_OLLAMA_MODEL
    assert body["prompt"] == "a prompt"
    assert body["stream"] is False
    assert body["options"]["temperature"] == smod.DEFAULT_TEMPERATURE
    assert body["options"]["num_predict"] == smod.DEFAULT_MAX_OUTPUT_TOKENS


def test_ollama_summarizer_strips_response_fences() -> None:
    response = _MockResponse({"response": "```\nclean output\n```"})
    s = smod.OllamaSummarizer(client=_MockClient(response))
    assert s.generate("p") == "clean output"


def test_ollama_summarizer_returns_empty_on_http_error() -> None:
    response = _MockResponse({}, status_code=503)
    s = smod.OllamaSummarizer(client=_MockClient(response))
    assert s.generate("p") == ""


def test_ollama_summarizer_returns_empty_on_missing_response_field() -> None:
    response = _MockResponse({"unexpected": "shape"})
    s = smod.OllamaSummarizer(client=_MockClient(response))
    # `body.get("response", "")` returns "" — strip_fences passes
    # through to the empty string.
    assert s.generate("p") == ""


def test_ollama_summarizer_honors_custom_model_and_temperature() -> None:
    client = _MockClient(_MockResponse({"response": "x"}))
    s = smod.OllamaSummarizer(
        client=client,
        model="custom:13b",
        temperature=0.5,
        max_output_tokens=1234,
    )
    s.generate("p")
    body = client.last_json
    assert body is not None
    assert body["model"] == "custom:13b"
    assert body["options"]["temperature"] == 0.5
    assert body["options"]["num_predict"] == 1234
    assert s.name == "ollama:custom:13b"


# ─── OpenAISummarizer ─────────────────────────────────────────────


def test_openai_summarizer_name_includes_model() -> None:
    s = smod.OpenAISummarizer(client=_MockClient(_MockResponse({"choices": []})))
    assert s.name == f"openai:{smod.DEFAULT_OPENAI_MODEL}"


def test_openai_summarizer_calls_chat_completions_endpoint() -> None:
    response = _MockResponse({"choices": [{"message": {"content": "openai summary"}}]})
    client = _MockClient(response)
    s = smod.OpenAISummarizer(client=client)

    out = s.generate("a prompt")

    assert out == "openai summary"
    assert client.last_path == "/chat/completions"
    body = client.last_json
    assert body is not None
    assert body["model"] == smod.DEFAULT_OPENAI_MODEL
    assert body["messages"] == [{"role": "user", "content": "a prompt"}]
    assert body["temperature"] == smod.DEFAULT_TEMPERATURE
    assert body["max_tokens"] == smod.DEFAULT_MAX_OUTPUT_TOKENS


def test_openai_summarizer_strips_response_fences() -> None:
    response = _MockResponse({"choices": [{"message": {"content": "```\nclean output\n```"}}]})
    s = smod.OpenAISummarizer(client=_MockClient(response))
    assert s.generate("p") == "clean output"


def test_openai_summarizer_returns_empty_on_http_error() -> None:
    response = _MockResponse({}, status_code=500)
    s = smod.OpenAISummarizer(client=_MockClient(response))
    assert s.generate("p") == ""


def test_openai_summarizer_returns_empty_on_no_choices() -> None:
    response = _MockResponse({"choices": []})
    s = smod.OpenAISummarizer(client=_MockClient(response))
    assert s.generate("p") == ""


def test_openai_summarizer_returns_empty_on_missing_content() -> None:
    response = _MockResponse({"choices": [{"message": {}}]})
    s = smod.OpenAISummarizer(client=_MockClient(response))
    assert s.generate("p") == ""


def test_openai_summarizer_honors_base_url_override() -> None:
    s = smod.OpenAISummarizer(
        client=_MockClient(_MockResponse({"choices": []})),
        base_url="https://api.together.xyz/v1/",
    )
    # Trailing slash gets stripped per __init__.
    assert s.base_url == "https://api.together.xyz/v1"


def test_openai_summarizer_lazy_import_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no client is passed and no API key is available, _http_client raises.

    This is the "explain to the user how to fix it" path — the error
    message must name both the env var and the auth CLI command.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Stub the auth lookup to return None for both env + file.
    import memstem.auth as auth_module

    monkeypatch.setattr(auth_module, "get_secret", lambda provider, env_var=None: "")
    s = smod.OpenAISummarizer()
    with pytest.raises(RuntimeError) as exc_info:
        s._http_client()
    msg = str(exc_info.value)
    assert "OPENAI_API_KEY" in msg
    assert "memstem auth set openai" in msg


# ─── End-to-end caching with a stub Summarizer ────────────────────


def test_summarizer_cache_isolates_by_summarizer_name(cache_db: sqlite3.Connection) -> None:
    """Two summarizers shouldn't share cache rows on the same prompt.

    A cached output from `openai:gpt-5.4-mini` must not be served
    when a request comes in via `ollama:qwen2.5:7b`. The cache key
    includes summarizer name precisely to prevent that.
    """
    a = smod.StubSummarizer()
    a.name = "summA"
    a.set_output("p", "from-a")

    b = smod.StubSummarizer()
    b.name = "summB"
    b.set_output("p", "from-b")

    out_a = a.generate_cached("p", db=cache_db)
    out_b = b.generate_cached("p", db=cache_db)

    assert out_a == "from-a"
    assert out_b == "from-b"
    rows = cache_db.execute("SELECT COUNT(*) FROM summarizer_cache").fetchone()[0]
    assert rows == 2
