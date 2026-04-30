"""Cross-encoder rerank for retrieval-time precision lift (ADR 0017).

After RRF + importance + (optional) MMR, the top-N is ordered by
fused inverse-rank score with a diversification penalty. None of those
signals look at word-level interactions between the query and the
document body — they only look at lexical overlap (BM25), bi-encoder
vector geometry (vec), and pairwise cosine (MMR). A cross-encoder
sees both sides of the boundary and produces a relevance score that
depends on cross-attention.

This module ships scaffolding plus a production OllamaReranker. The
``Reranker`` ABC mirrors the ``DedupJudge`` pattern from ADR 0012:

- :class:`NoOpReranker` — silent fallback; every score is ``1.0`` so
  stable sort preserves the input order. Wiring rerank with NoOp is a
  no-op at the ranking level.
- :class:`StubReranker` — in-memory verdicts for tests; matches the
  ``StubJudge`` shape from dedup_judge.
- :class:`OllamaReranker` — production reranker. LLM-as-judge via
  ``/api/generate`` with a ``[0, 100]`` integer scoring prompt,
  normalized to ``[0, 1]``.

Caching: every score computed by a non-NoOp reranker is written to
``rerank_cache`` keyed on ``(query_hash, memory_id, body_hash, judge)``.
Cache hits skip the LLM round trip entirely. See ADR 0017 §Cache for
the schema and rationale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from memstem.core.storage import Memory

logger = logging.getLogger(__name__)

DEFAULT_RERANK_TOP_N = 20
"""Default candidate-pool size for rerank when enabled.

ADR 0017 §Cost: literature ideal is top-50, but with LLM-as-judge the
cold-cache latency on 50 candidates is ~7.5s per query. top-20 keeps
cold-cache p95 in the 3s range and steady-state p95 well under
500 ms. Callers can override at the search-call site."""

DEFAULT_OLLAMA_URL = "http://localhost:11434"
"""Default Ollama HTTP endpoint. Production deployments typically
override via the constructor; the default matches the dedup_judge
default for operational consistency."""

DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
"""Default model. Matches the dedup_judge default so the same
already-pulled model serves both features. Swappable via the
``OllamaReranker(model=...)`` kwarg."""

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
"""Default OpenAI-compatible base URL. Override the constructor to
point at any OpenAI-compatible endpoint (Together, LM Studio, etc.)."""

DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
"""Default env var checked for the OpenAI API key. The auth module
also falls back to ``~/.config/memstem/secrets.yaml``."""

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
"""Default OpenAI model. See ``docs/recall-models.md`` for the
recommended-models ladder and upgrade path."""

MAX_RERANK_BODY_CHARS = 4000
"""Hard cap on document body characters sent to the rerank LLM.

Cross-encoders judge relevance from the first ~1k tokens of a
document; sending more wastes tokens and (on small-context models)
overflows the context window outright. Brad's vault contains
multi-megabyte ``Infrastructure — Extended Context``-style memories
that dwarf any model's context window — without truncation those
candidates uniformly return 400 Bad Request from OpenAI and
silently fall to the bottom of the rerank order.

4000 chars is roughly 1000 tokens — fits inside every supported
provider's context with room for the prompt template. Truncated
bodies get a ``[…document continues for N more chars]`` marker so
the LLM knows it's looking at a slice, not the whole thing.

This cap is at prompt-build time; the cache key still uses the
full body's hash so a body edit invalidates the cached score
correctly."""


def _format_body_for_prompt(body: str) -> str:
    """Truncate a document body for inclusion in a rerank prompt.

    Returns the body unchanged if it's already short enough.
    Otherwise: returns the leading slice plus a continuation marker
    so the LLM knows it's seeing a truncated document.
    """
    if len(body) <= MAX_RERANK_BODY_CHARS:
        return body
    head = body[:MAX_RERANK_BODY_CHARS]
    remaining = len(body) - MAX_RERANK_BODY_CHARS
    return f"{head}\n\n[…document continues for {remaining:,} more chars]"


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """One candidate's rerank-relevant payload.

    The body hash is captured at construction so the cache key is
    stable across calls — recomputing the hash inline is cheap (SHA-256
    of a few KB of UTF-8) and avoids threading hashes through callers.
    """

    memory_id: str
    title: str
    body: str
    body_hash: str

    @classmethod
    def from_memory(cls, memory: Memory) -> RerankCandidate:
        """Build a candidate from a materialized :class:`Memory`."""
        body = memory.body
        return cls(
            memory_id=str(memory.id),
            title=memory.frontmatter.title or "",
            body=body,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )


def query_hash(query: str) -> str:
    """SHA-256 hex of the raw query string. The cache's first key column."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def cache_lookup(
    db: sqlite3.Connection,
    *,
    qhash: str,
    memory_id: str,
    body_hash: str,
    judge: str,
) -> float | None:
    """Return the cached score for the four-tuple key, or ``None`` on miss.

    SQLite errors are logged and treated as cache miss — a corrupt or
    locked cache should never break the search path.
    """
    try:
        row = db.execute(
            """
            SELECT score FROM rerank_cache
            WHERE query_hash = ? AND memory_id = ? AND body_hash = ? AND judge = ?
            """,
            (qhash, memory_id, body_hash, judge),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("rerank_cache: lookup failed: %s", exc)
        return None
    if row is None:
        return None
    score = row["score"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def cache_write(
    db: sqlite3.Connection,
    *,
    qhash: str,
    memory_id: str,
    body_hash: str,
    judge: str,
    score: float,
    now: datetime | None = None,
) -> None:
    """Upsert one ``(query, memory, body_hash, judge) -> score`` row.

    Uses ``INSERT ... ON CONFLICT`` so cache writes are idempotent.
    Failures are logged and swallowed; cache is non-canonical.
    """
    ts = (now or datetime.now(tz=UTC)).isoformat()
    try:
        with db:
            db.execute(
                """
                INSERT INTO rerank_cache
                    (query_hash, memory_id, body_hash, judge, score, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_hash, memory_id, body_hash, judge)
                DO UPDATE SET score = excluded.score, ts = excluded.ts
                """,
                (qhash, memory_id, body_hash, judge, score, ts),
            )
    except sqlite3.Error as exc:
        logger.warning("rerank_cache: write failed: %s", exc)


class Reranker(ABC):
    """Abstract base for cross-encoder rerankers.

    Subclasses override :meth:`score` to produce one query-document
    relevance score in ``[0, 1]``. The :meth:`score_candidates`
    orchestrator wraps each call in a cache lookup so repeat scores on
    unchanged content skip the model.

    Subclasses MUST set :attr:`name` to a stable identifier that ends
    up in the cache's ``judge`` column. Different models or prompt
    variants must have different names so cache rows don't collide.
    """

    name: str = "abstract"

    @abstractmethod
    def score(self, query: str, candidate: RerankCandidate) -> float:
        """Return one relevance score in ``[0, 1]``."""

    def score_candidates(
        self,
        query: str,
        candidates: Iterable[RerankCandidate],
        db: sqlite3.Connection | None = None,
    ) -> list[float]:
        """Score each candidate, consulting the cache when ``db`` is provided.

        Order of operations per candidate:

        1. Cache lookup on ``(query_hash, memory_id, body_hash, name)``.
        2. On miss, call :meth:`score` and cache the result.

        The cache is bypassed when ``db is None`` (used by tests that
        don't want to round-trip through SQLite) and by NoOp (which
        overrides this method for the trivial path).
        """
        candidates = list(candidates)
        if not candidates:
            return []
        scores: list[float] = []
        qhash = query_hash(query)
        for candidate in candidates:
            cached: float | None = None
            if db is not None:
                cached = cache_lookup(
                    db,
                    qhash=qhash,
                    memory_id=candidate.memory_id,
                    body_hash=candidate.body_hash,
                    judge=self.name,
                )
            if cached is not None:
                scores.append(cached)
                continue
            try:
                value = self.score(query, candidate)
            except Exception as exc:
                logger.warning(
                    "Reranker(%s): score failed for %s: %s",
                    self.name,
                    candidate.memory_id,
                    exc,
                )
                # Score 0.0 means "no opinion expressed" under the
                # downstream sort — the candidate's RRF tiebreak still
                # gives it a relative position.
                value = 0.0
            value = max(0.0, min(1.0, value))
            scores.append(value)
            if db is not None:
                cache_write(
                    db,
                    qhash=qhash,
                    memory_id=candidate.memory_id,
                    body_hash=candidate.body_hash,
                    judge=self.name,
                    score=value,
                )
        return scores


class NoOpReranker(Reranker):
    """Default fallback reranker — every score is ``1.0``.

    Wiring rerank with NoOp is a no-op at the ranking level: every
    candidate gets the same score, and the downstream stable sort
    preserves the RRF + importance order. Used as the silent fallback
    when ``rerank_top_n`` is set but no real reranker is configured.
    """

    name = "noop"

    def score(self, query: str, candidate: RerankCandidate) -> float:
        return 1.0

    def score_candidates(
        self,
        query: str,
        candidates: Iterable[RerankCandidate],
        db: sqlite3.Connection | None = None,
    ) -> list[float]:
        # Skip the cache entirely — NoOp's score is constant and free,
        # so caching it would waste rows and writes.
        return [1.0 for _ in candidates]


class StubReranker(Reranker):
    """In-memory reranker for tests.

    Tests register canned ``(query, memory_id) -> score`` entries via
    :meth:`set_score`; the orchestration receives exactly those scores.
    Mirrors :class:`memstem.hygiene.dedup_judge.StubJudge` so the
    test-fixture muscle memory transfers.
    """

    name = "stub"

    def __init__(self) -> None:
        self._scores: dict[tuple[str, str], float] = {}
        self._default = 0.5

    def set_score(self, query: str, memory_id: str, score: float) -> None:
        """Configure the score the stub will return for one pair."""
        self._scores[(query, memory_id)] = score

    def set_default(self, score: float) -> None:
        """Default score for any pair not registered with :meth:`set_score`."""
        self._default = score

    def score(self, query: str, candidate: RerankCandidate) -> float:
        return self._scores.get((query, candidate.memory_id), self._default)


def _load_rerank_prompt() -> str:
    """Read the canonical OllamaReranker prompt template from package data.

    Lives next to ``dedup_judge.txt`` under ``memstem/prompts/``.
    """
    path = Path(__file__).parent.parent / "prompts" / "rerank.txt"
    return path.read_text(encoding="utf-8")


_INTEGER_RE = re.compile(r"-?\d+")


def _parse_score(text: str) -> float:
    """Permissively parse a 0-100 integer score from an Ollama response.

    Accepts:
        - bare integer ("85")
        - JSON object ('{"score": 60}')
        - "Score: 70" / "Score is 42 because ..."

    Out-of-range values are clamped. Anything unparseable returns
    ``0.0`` (the candidate keeps its RRF tiebreak position) and the
    caller logs the raw response.
    """
    if not text:
        return 0.0
    stripped = text.strip()
    # Try JSON object first; ignore parse failures and fall through.
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            raw = data.get("score")
            if raw is not None:
                value = float(raw)
                return max(0.0, min(1.0, value / 100.0))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Find the first integer in the text — handles "Score: 60",
    # "60/100", "60", and many other shapes the LLM might produce.
    match = _INTEGER_RE.search(stripped)
    if match is None:
        return 0.0
    try:
        value = float(match.group(0))
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, value / 100.0))


class OllamaReranker(Reranker):
    """Production reranker. Calls a local Ollama model with a relevance prompt.

    The model is asked to return a ``[0, 100]`` integer score for
    ``(query, document)``. The response is parsed permissively (bare
    integer, JSON, or "Score: N" prose), normalized to ``[0, 1]``,
    and clamped. HTTP failures are logged; the candidate falls back
    to ``0.0`` for that score (the RRF tiebreak still positions it).

    The constructor accepts an explicit ``client`` callable so tests
    can mock the HTTP layer. Production paths lazy-import ``httpx``
    so the module stays cheap to import in environments without a
    live Ollama.
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
        self.prompt_template = prompt_template or _load_rerank_prompt()
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            # Lazy httpx import keeps `import memstem.core.rerank` cheap
            # in test environments and on machines without httpx wired
            # up. httpx is already a runtime dep (see pyproject.toml).
            import httpx

            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def score(self, query: str, candidate: RerankCandidate) -> float:
        prompt = self.prompt_template.format(
            query=query,
            title=candidate.title or candidate.memory_id,
            body=_format_body_for_prompt(candidate.body),
        )
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning(
                "OllamaReranker: model call failed for %s: %s",
                candidate.memory_id,
                exc,
            )
            return 0.0
        return _parse_score(response)

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        post = client.post  # type: ignore[attr-defined]
        result = post(
            "/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                # Low temperature for scoring stability; we want the
                # same query-doc pair to produce the same number on
                # repeat calls, not creative variation.
                "options": {"temperature": 0.0, "num_predict": 16},
            },
        )
        result.raise_for_status()
        body = result.json()
        return str(body.get("response", ""))


class OpenAIReranker(Reranker):
    """OpenAI-compatible reranker. Calls the chat-completions endpoint.

    Talks to ``{base_url}/chat/completions`` with the standard OpenAI
    shape (``model`` + ``messages``). The default ``base_url`` is
    OpenAI itself, but any compatible provider works — set
    ``base_url`` to e.g. ``https://api.together.xyz/v1`` for Together,
    ``http://localhost:1234/v1`` for LM Studio, etc.

    Recommended model: ``gpt-4o-mini``. See
    ``docs/recall-models.md`` for the full upgrade ladder.

    The API key is read via :mod:`memstem.auth` — env var first,
    ``~/.config/memstem/secrets.yaml`` second. Failures (auth, HTTP,
    parse) fall back to ``0.0`` for that candidate; the search path
    keeps moving.
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
        self.prompt_template = prompt_template or _load_rerank_prompt()
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            # Lazy imports keep the module cheap to load when the
            # OpenAI variant isn't in use. ``memstem.auth`` is a leaf
            # module with no project deps, so no cycle risk.
            import httpx

            from memstem.auth import get_secret

            api_key = get_secret("openai", env_var=self.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"OpenAIReranker needs an API key. Either export "
                    f"${self.api_key_env}, run "
                    f"`memstem auth set openai <key>`, or use "
                    f"OllamaReranker for local-only setups."
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

    def score(self, query: str, candidate: RerankCandidate) -> float:
        prompt = self.prompt_template.format(
            query=query,
            title=candidate.title or candidate.memory_id,
            body=_format_body_for_prompt(candidate.body),
        )
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning(
                "OpenAIReranker: model call failed for %s: %s",
                candidate.memory_id,
                exc,
            )
            return 0.0
        return _parse_score(response)

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        post = client.post  # type: ignore[attr-defined]
        result = post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                # Low temperature for scoring stability; ``max_tokens``
                # is small because we're asking for an integer, not a
                # paragraph.
                "temperature": 0.0,
                "max_tokens": 16,
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
    "DEFAULT_RERANK_TOP_N",
    "MAX_RERANK_BODY_CHARS",
    "NoOpReranker",
    "OllamaReranker",
    "OpenAIReranker",
    "RerankCandidate",
    "Reranker",
    "StubReranker",
    "cache_lookup",
    "cache_write",
    "query_hash",
]
