"""HyDE query expansion at retrieval time (ADR 0018).

The bi-encoder embeds query and document independently, so vec
retrieval works only when the query and the answer share embedding-
space neighbors. The classic failure is the procedural-query /
declarative-answer mismatch: "how do I send a Telegram message"
doesn't share vocabulary with "use ``bash ~/scripts/tg-send 'msg'``"
even though one is the literal answer to the other.

HyDE (Hypothetical Document Embeddings) routes around this by asking
an LLM to write a passage that *would* answer the query, then embeds
that passage instead of the original query. The hypothesis shares
vocabulary with the real answer in the vault, even when the original
query doesn't — and the LLM doesn't need to be right about the facts,
just produce passage-shaped text in the right region of embedding
space.

This module ships scaffolding plus a production OllamaExpander. The
:class:`HydeExpander` ABC mirrors the ``DedupJudge`` (ADR 0012) and
``Reranker`` (ADR 0017) patterns:

- :class:`NoOpExpander` — silent fallback; returns the query
  unchanged. Wiring HyDE with NoOp is a no-op at the retrieval level.
- :class:`StubExpander` — in-memory hypotheses for tests.
- :class:`OllamaExpander` — production expander. Calls Ollama
  ``/api/generate`` with a passage-generation prompt and returns the
  trimmed response.

Caching: every hypothesis computed by a non-NoOp expander is written
to ``hyde_cache`` keyed on ``(query_hash, judge)``. Cache hits skip
the LLM. See ADR 0018 §Cache for the schema and rationale.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
"""Default Ollama HTTP endpoint. Matches the dedup_judge / reranker
default for operational consistency."""

DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
"""Default model. Same model the dedup judge and reranker use so a
single already-pulled model serves all three features."""

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
"""Default OpenAI-compatible base URL. Override the constructor to
point at any OpenAI-compatible endpoint."""

DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
"""Default env var checked for the OpenAI API key. The auth module
also falls back to ``~/.config/memstem/secrets.yaml``."""

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
"""Default OpenAI model. See ``docs/recall-models.md`` for the
recommended-models ladder and upgrade path."""

MIN_QUERY_TOKENS = 3
"""Minimum query token count for ``should_expand``. Queries shorter
than this are typically exact lookups (``"ari port"``,
``"rrf k"``) where the original query already shares vocabulary
with the answer. HyDE adds noise, not signal."""

_QUOTE_RE = re.compile(r'"[^"]+"')
_BOOL_OP_RE = re.compile(r"\b(AND|OR|NOT)\b")
_BOOL_PREFIX_RE = re.compile(r"(^|\s)[+-]\w")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{32,}$")
_PATH_RE = re.compile(r"^[/~][\w./~\-]+$|^[\w\-]+/[\w./\-]+$")


def query_hash(query: str) -> str:
    """SHA-256 hex of the raw query string. The cache's first key column."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def cache_lookup(
    db: sqlite3.Connection,
    *,
    qhash: str,
    judge: str,
) -> str | None:
    """Return the cached hypothesis for the (query, judge) pair, or ``None``.

    SQLite errors are logged and treated as cache miss — a corrupt or
    locked cache should never break the search path.
    """
    try:
        row = db.execute(
            "SELECT hypothesis FROM hyde_cache WHERE query_hash = ? AND judge = ?",
            (qhash, judge),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("hyde_cache: lookup failed: %s", exc)
        return None
    if row is None:
        return None
    hypothesis = row["hypothesis"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(hypothesis, str):
        return None
    return hypothesis


def cache_write(
    db: sqlite3.Connection,
    *,
    qhash: str,
    judge: str,
    hypothesis: str,
    now: datetime | None = None,
) -> None:
    """Upsert one ``(query, judge) -> hypothesis`` row.

    Idempotent via ``INSERT ... ON CONFLICT``. Failures are logged and
    swallowed; the cache is non-canonical.
    """
    ts = (now or datetime.now(tz=UTC)).isoformat()
    try:
        with db:
            db.execute(
                """
                INSERT INTO hyde_cache (query_hash, judge, hypothesis, ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(query_hash, judge)
                DO UPDATE SET hypothesis = excluded.hypothesis, ts = excluded.ts
                """,
                (qhash, judge, hypothesis, ts),
            )
    except sqlite3.Error as exc:
        logger.warning("hyde_cache: write failed: %s", exc)


def should_expand(query: str) -> bool:
    """Return ``True`` iff the query benefits from hypothetical expansion.

    Conservative by default: HyDE adds latency and an LLM round trip
    per query, so we want to skip queries that don't benefit. The
    gates check four common "exact lookup" patterns:

    1. Length: queries with fewer than :data:`MIN_QUERY_TOKENS` words
       are typically exact lookups (``"ari port"``, ``"rrf k"``).
    2. Quoted strings: the user signaled exact-match intent.
    3. Boolean operators: structured queries don't compose with
       hypothesis-based expansion.
    4. Identifier shapes: a UUID, hex hash, or file path is an exact
       reference, not a question.
    """
    if not query or not query.strip():
        return False
    stripped = query.strip()
    if _UUID_RE.match(stripped):
        return False
    if _HEX_HASH_RE.match(stripped):
        return False
    if _PATH_RE.match(stripped):
        return False
    if _QUOTE_RE.search(stripped):
        return False
    if _BOOL_OP_RE.search(stripped) or _BOOL_PREFIX_RE.search(stripped):
        return False
    tokens = [t for t in re.split(r"\s+", stripped) if t]
    if len(tokens) < MIN_QUERY_TOKENS:
        return False
    return True


class HydeExpander(ABC):
    """Abstract base for query-expansion expanders.

    Subclasses override :meth:`expand` to produce one hypothetical
    passage for the query. The :meth:`expand_cached` orchestrator
    wraps the call in a cache lookup so repeat queries skip the LLM.

    Subclasses MUST set :attr:`name` to a stable identifier that ends
    up in the cache's ``judge`` column.
    """

    name: str = "abstract"

    @abstractmethod
    def expand(self, query: str) -> str:
        """Return a hypothetical-answer passage for ``query``.

        Implementations should return the empty string on failure;
        callers fall back to the original query in that case.
        """

    def should_expand(self, query: str) -> bool:
        """Default gate. Implementations can override for finer control."""
        return should_expand(query)

    def expand_cached(
        self,
        query: str,
        db: sqlite3.Connection | None = None,
    ) -> str:
        """Expand with cache-aware orchestration.

        Order of operations:

        1. Cache lookup on ``(query_hash, name)``.
        2. On miss, call :meth:`expand`, then cache the result.

        The cache is bypassed when ``db is None`` (used by tests that
        don't want to round-trip through SQLite) and by NoOp (which
        overrides this method for the trivial path).
        """
        qhash = query_hash(query)
        if db is not None:
            cached = cache_lookup(db, qhash=qhash, judge=self.name)
            if cached is not None:
                return cached
        try:
            hypothesis = self.expand(query)
        except Exception as exc:
            logger.warning("HydeExpander(%s): expand failed: %s", self.name, exc)
            return ""
        if not isinstance(hypothesis, str):
            return ""
        if db is not None and hypothesis:
            cache_write(db, qhash=qhash, judge=self.name, hypothesis=hypothesis)
        return hypothesis


class NoOpExpander(HydeExpander):
    """Default fallback expander — returns the query unchanged.

    Wiring HyDE with NoOp is a no-op at the retrieval level: the vec
    query embedding is built from the original query, exactly as
    pre-HyDE. Used as the silent fallback when ``use_hyde=True`` is
    set on a :class:`Search` without a configured expander.
    """

    name = "noop"

    def expand(self, query: str) -> str:
        return query

    def expand_cached(
        self,
        query: str,
        db: sqlite3.Connection | None = None,
    ) -> str:
        # Skip the cache entirely — NoOp's output is constant and free,
        # so caching it would waste rows and writes.
        return query


class StubExpander(HydeExpander):
    """In-memory expander for tests.

    Tests register canned ``query -> hypothesis`` entries via
    :meth:`set_hypothesis`; the orchestration receives exactly those
    hypotheses. Mirrors :class:`memstem.core.rerank.StubReranker`.
    """

    name = "stub"

    def __init__(self) -> None:
        self._hypotheses: dict[str, str] = {}
        self._default = ""

    def set_hypothesis(self, query: str, hypothesis: str) -> None:
        """Configure the hypothesis the stub will return for one query."""
        self._hypotheses[query] = hypothesis

    def set_default(self, hypothesis: str) -> None:
        """Default hypothesis for any query not registered."""
        self._default = hypothesis

    def expand(self, query: str) -> str:
        return self._hypotheses.get(query, self._default)


def _load_hyde_prompt() -> str:
    """Read the canonical OllamaExpander prompt template from package data."""
    path = Path(__file__).parent.parent / "prompts" / "hyde.txt"
    return path.read_text(encoding="utf-8")


def _strip_fences(text: str) -> str:
    """Trim leading/trailing whitespace and surrounding code fences.

    LLMs occasionally wrap output in ``` fences. We strip them so the
    embedder sees clean prose.
    """
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        # Drop the first line (which contains the fence + optional lang)
        # and the trailing fence line.
        lines = stripped.split("\n")
        if len(lines) >= 2:
            inner = "\n".join(lines[1:-1])
            return inner.strip()
    return stripped


class OllamaExpander(HydeExpander):
    """Production expander. Calls a local Ollama model with a passage prompt.

    The model is asked to write a one-paragraph hypothetical answer to
    the query. The response is trimmed and returned as-is — the
    embedder will reduce it to a vector, so syntactic cleanliness
    isn't required as long as the *vocabulary* is right.

    HTTP failures are logged; the caller (search) detects an empty
    return and falls back to the original query for embedding.

    The constructor accepts an explicit ``client`` callable so tests
    can mock the HTTP layer. Production paths lazy-import ``httpx``.
    """

    name_prefix = "ollama"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        prompt_template: str | None = None,
        timeout: float = 60.0,
        client: object = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.prompt_template = prompt_template or _load_hyde_prompt()
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            # Lazy httpx import — same pattern as OllamaReranker /
            # OllamaDedupJudge. httpx is a runtime dep already.
            import httpx

            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def expand(self, query: str) -> str:
        prompt = self.prompt_template.format(query=query)
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning("OllamaExpander: model call failed: %s", exc)
            return ""
        return _strip_fences(response)

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        post = client.post  # type: ignore[attr-defined]
        result = post(
            "/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                # Mild temperature: we want passage-shaped text, not
                # deterministic recitation. ~150 tokens fits one
                # paragraph; longer wastes embedding context.
                "options": {"temperature": 0.3, "num_predict": 200},
            },
        )
        result.raise_for_status()
        body = result.json()
        return str(body.get("response", ""))


class OpenAIExpander(HydeExpander):
    """OpenAI-compatible HyDE expander. Calls the chat-completions endpoint.

    Talks to ``{base_url}/chat/completions`` with the standard OpenAI
    shape. The default ``base_url`` is OpenAI itself; any compatible
    provider works.

    Recommended model: ``gpt-4o-mini``. See ``docs/recall-models.md``
    for the upgrade ladder.

    HTTP failures return ``""``; the search path detects that and
    falls back to the original query for embedding.
    """

    name_prefix = "openai"

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        api_key_env: str = DEFAULT_OPENAI_API_KEY_ENV,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        prompt_template: str | None = None,
        timeout: float = 60.0,
        client: object = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.prompt_template = prompt_template or _load_hyde_prompt()
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            import httpx

            from memstem.auth import get_secret

            api_key = get_secret("openai", env_var=self.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"OpenAIExpander needs an API key. Either export "
                    f"${self.api_key_env}, run "
                    f"`memstem auth set openai <key>`, or use "
                    f"OllamaExpander for local-only setups."
                )
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def expand(self, query: str) -> str:
        prompt = self.prompt_template.format(query=query)
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning("OpenAIExpander: model call failed: %s", exc)
            return ""
        return _strip_fences(response)

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        post = client.post  # type: ignore[attr-defined]
        result = post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                # Mild temperature: passage-shaped text, not
                # deterministic recitation. ``max_tokens`` is sized
                # for one paragraph (~150 tokens).
                "temperature": 0.3,
                "max_tokens": 200,
            },
        )
        result.raise_for_status()
        body = result.json()
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        return str(content) if content is not None else ""


__all__ = [
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OPENAI_API_KEY_ENV",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "MIN_QUERY_TOKENS",
    "HydeExpander",
    "NoOpExpander",
    "OllamaExpander",
    "OpenAIExpander",
    "StubExpander",
    "cache_lookup",
    "cache_write",
    "query_hash",
    "should_expand",
]
