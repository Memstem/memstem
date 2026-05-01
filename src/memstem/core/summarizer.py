"""Generic LLM summarizer abstraction (ADRs 0020 + 0021).

The session-distillation writer (ADR 0020) and the project-records
writer (ADR 0021) both need the same primitive: hand a prompt to an
LLM, get back a paragraph or two of structured prose. This module
ships that primitive once so both writers can share it.

Mirrors the rerank / HyDE / dedup-judge pattern from ADRs
0012/0017/0018:

- :class:`NoOpSummarizer` — silent fallback; returns ``""`` so the
  caller can detect "no LLM configured" and skip the candidate.
  Wiring distillation with NoOp is a no-op at the writer level.
- :class:`StubSummarizer` — in-memory canned outputs for tests.
- :class:`OllamaSummarizer` — production summarizer for local
  installs. Calls ``/api/generate`` with a longer ``num_predict``
  budget than HyDE (summaries are paragraph-shaped, not 1-line).
- :class:`OpenAISummarizer` — production summarizer for cloud
  installs. Calls ``/chat/completions`` with the standard OpenAI
  shape; default model is ``gpt-5.4-mini`` because summary text is
  the search target and quality matters more than for rerank/HyDE.

Caching: every output computed by a non-NoOp summarizer is written to
``summarizer_cache`` keyed on ``(content_hash, summarizer)``. The
``content_hash`` is the SHA-256 of the *full prompt* the writer
constructed — including its template and all interpolated fields —
so any change to template or input invalidates the cache row
correctly. Cache hits skip the LLM round trip entirely.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
"""Default Ollama HTTP endpoint. Matches the dedup_judge / reranker
/ hyde default for operational consistency."""

DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
"""Default Ollama model. Same model the rest of MemStem's local
LLM features use so a single already-pulled model serves all of
rerank, HyDE, dedup-judge, and summarization."""

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
"""Default OpenAI-compatible base URL. Override the constructor to
point at any OpenAI-compatible endpoint (Together, LM Studio, vLLM,
etc.)."""

DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
"""Default env var checked for the OpenAI API key. The auth module
also falls back to ``~/.config/memstem/secrets.yaml``."""

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
"""Default OpenAI model. ADR 0020 §LLM choice: the summary text *is*
the search target, so quality matters more than for rerank/HyDE
(where summary text isn't indexed). ``gpt-5.4-mini`` is the current
mini-tier model with the best summarization-task performance per
dollar; see ``docs/recall-models.md`` for the upgrade ladder."""

DEFAULT_TEMPERATURE = 0.2
"""Low-but-not-zero temperature: we want consistent summaries on
repeat calls without being so deterministic that the model misses
slight phrasing improvements. Higher values produce wider
variance — fine for HyDE (passage-shaped text), bad for summaries
(the output is what gets indexed)."""

DEFAULT_MAX_OUTPUT_TOKENS = 800
"""Max output tokens. Sized for a 1-paragraph summary plus the
structured "Key entities / deliverables / decisions" section in
``prompts/distill_session.txt`` and ``prompts/distill_project.txt``.
Both templates produce well under 800 tokens of well-formed output;
this gives headroom without uncapped cost."""


def content_hash(prompt: str) -> str:
    """SHA-256 hex of the full prompt. Cache key column for ``summarizer_cache``.

    The full prompt is hashed (not just the input fields) so a change
    to the prompt template invalidates every cached output that was
    produced under the old template. This is the right invalidation
    behavior because two prompts that differ only in template wording
    can produce materially different summaries.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def cache_lookup(
    db: sqlite3.Connection,
    *,
    chash: str,
    summarizer: str,
) -> str | None:
    """Return the cached output for the (content, summarizer) pair, or ``None``.

    SQLite errors are logged and treated as cache miss — a corrupt or
    locked cache should never break the writer path.
    """
    try:
        row = db.execute(
            "SELECT output FROM summarizer_cache WHERE content_hash = ? AND summarizer = ?",
            (chash, summarizer),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("summarizer_cache: lookup failed: %s", exc)
        return None
    if row is None:
        return None
    output = row["output"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(output, str):
        return None
    return output


def cache_write(
    db: sqlite3.Connection,
    *,
    chash: str,
    summarizer: str,
    output: str,
    now: datetime | None = None,
) -> None:
    """Upsert one ``(content, summarizer) -> output`` row.

    Idempotent via ``INSERT ... ON CONFLICT``. Failures are logged
    and swallowed; the cache is non-canonical (drop-and-rebuild
    safe) so a write failure should never break the writer path.
    """
    ts = (now or datetime.now(tz=UTC)).isoformat()
    try:
        with db:
            db.execute(
                """
                INSERT INTO summarizer_cache (content_hash, summarizer, output, ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(content_hash, summarizer)
                DO UPDATE SET output = excluded.output, ts = excluded.ts
                """,
                (chash, summarizer, output, ts),
            )
    except sqlite3.Error as exc:
        logger.warning("summarizer_cache: write failed: %s", exc)


class Summarizer(ABC):
    """Abstract base for LLM summarizers.

    Subclasses override :meth:`generate` to produce output text for a
    given prompt. The :meth:`generate_cached` orchestrator wraps the
    call in a cache lookup so repeat prompts skip the LLM.

    Subclasses MUST set :attr:`name` to a stable identifier that ends
    up in the cache's ``summarizer`` column. The convention is
    ``"<provider>:<model>"`` (e.g. ``"openai:gpt-5.4-mini"``,
    ``"ollama:qwen2.5:7b"``) so different providers or models don't
    collide on cache rows.
    """

    name: str = "abstract"

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return generated text for ``prompt``.

        Implementations should return the empty string on failure;
        callers detect the empty return and treat it as "skip this
        candidate" (logged at INFO).
        """

    def generate_cached(
        self,
        prompt: str,
        db: sqlite3.Connection | None = None,
    ) -> str:
        """Generate with cache-aware orchestration.

        Order of operations:

        1. Cache lookup on ``(content_hash, name)``.
        2. On miss, call :meth:`generate`, then cache the result.

        The cache is bypassed when ``db is None`` (used by tests that
        don't want to round-trip through SQLite) and by NoOp (which
        overrides this method for the trivial path).
        """
        chash = content_hash(prompt)
        if db is not None:
            cached = cache_lookup(db, chash=chash, summarizer=self.name)
            if cached is not None:
                return cached
        try:
            output = self.generate(prompt)
        except Exception as exc:
            logger.warning("Summarizer(%s): generate failed: %s", self.name, exc)
            return ""
        if not isinstance(output, str):
            return ""
        if db is not None and output:
            cache_write(db, chash=chash, summarizer=self.name, output=output)
        return output


class NoOpSummarizer(Summarizer):
    """Default fallback summarizer — always returns the empty string.

    Wiring distillation with NoOp is a no-op at the writer level: every
    candidate produces an empty output, the writer detects empty and
    skips the candidate (logged at INFO). Used as the silent fallback
    when distillation is enabled but no real summarizer is configured.
    """

    name = "noop"

    def generate(self, prompt: str) -> str:
        return ""

    def generate_cached(
        self,
        prompt: str,
        db: sqlite3.Connection | None = None,
    ) -> str:
        # Skip the cache entirely — NoOp's output is constant and free,
        # so caching it would waste rows and writes.
        return ""


class StubSummarizer(Summarizer):
    """In-memory summarizer for tests.

    Tests register canned ``prompt -> output`` entries via
    :meth:`set_output`; the orchestration receives exactly those
    outputs. Mirrors :class:`memstem.core.hyde.StubExpander` and
    :class:`memstem.core.rerank.StubReranker` so the test-fixture
    muscle memory transfers.

    The default output (used when a prompt isn't pre-registered) is
    the empty string by default — this matches the way the writer
    treats "no opinion" — but tests can flip it to a fixed sentinel
    for happy-path coverage.
    """

    name = "stub"

    def __init__(self) -> None:
        self._outputs: dict[str, str] = {}
        self._default = ""

    def set_output(self, prompt: str, output: str) -> None:
        """Configure the output the stub will return for one prompt."""
        self._outputs[prompt] = output

    def set_default(self, output: str) -> None:
        """Default output for any prompt not registered."""
        self._default = output

    def generate(self, prompt: str) -> str:
        return self._outputs.get(prompt, self._default)


def _strip_fences(text: str) -> str:
    """Trim surrounding code fences from an LLM response, if present.

    LLMs occasionally wrap output in ``` fences even when the prompt
    explicitly forbids it. Strip them so the writer sees clean prose.
    """
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.split("\n")
        if len(lines) >= 2:
            inner = "\n".join(lines[1:-1])
            return inner.strip()
    return stripped


class OllamaSummarizer(Summarizer):
    """Production summarizer. Calls a local Ollama model with a generation prompt.

    The prompt is passed to ``/api/generate`` with a paragraph-sized
    ``num_predict`` budget and a low temperature for consistent output.
    HTTP failures are logged; the writer detects an empty return and
    skips the candidate.

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
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = 120.0,
        client: object = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            # Lazy httpx import — same pattern as OllamaReranker /
            # OllamaExpander. httpx is a runtime dep already.
            import httpx

            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def generate(self, prompt: str) -> str:
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning("OllamaSummarizer: model call failed: %s", exc)
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
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_output_tokens,
                },
            },
        )
        result.raise_for_status()
        body = result.json()
        return str(body.get("response", ""))


class OpenAISummarizer(Summarizer):
    """OpenAI-compatible summarizer. Calls the chat-completions endpoint.

    Talks to ``{base_url}/chat/completions`` with the standard OpenAI
    shape (``model`` + ``messages``). The default ``base_url`` is
    OpenAI itself; any compatible provider works (Together, LM Studio,
    vLLM, etc.).

    Default model: ``gpt-5.4-mini``. ADR 0020 §LLM choice: summary
    text is the search target, so quality matters more than for
    rerank/HyDE. See ``docs/recall-models.md`` for the upgrade ladder.

    The API key is read via :mod:`memstem.auth` — env var first,
    ``~/.config/memstem/secrets.yaml`` second. Auth/HTTP/parse
    failures return ``""``; the writer treats that as "skip this
    candidate" without crashing.
    """

    name_prefix = "openai"

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        api_key_env: str = DEFAULT_OPENAI_API_KEY_ENV,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = 120.0,
        client: object = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
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
                    f"OpenAISummarizer needs an API key. Either export "
                    f"${self.api_key_env}, run "
                    f"`memstem auth set openai <key>`, or use "
                    f"OllamaSummarizer for local-only setups."
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

    def generate(self, prompt: str) -> str:
        try:
            response = self._call_model(prompt)
        except Exception as exc:
            logger.warning("OpenAISummarizer: model call failed: %s", exc)
            return ""
        return _strip_fences(response)

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        post = client.post  # type: ignore[attr-defined]
        # `max_completion_tokens` rather than `max_tokens` because the
        # GPT-5.x family rejects `max_tokens` outright (HTTP 400,
        # ``unsupported_parameter``). The newer field name is also
        # accepted by the older `gpt-4o-mini` family, so always sending
        # it avoids a model-name → field-name branch and keeps the
        # client forward-compatible with the next OpenAI rev.
        result = post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_completion_tokens": self.max_output_tokens,
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
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OPENAI_API_KEY_ENV",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_TEMPERATURE",
    "NoOpSummarizer",
    "OllamaSummarizer",
    "OpenAISummarizer",
    "StubSummarizer",
    "Summarizer",
    "cache_lookup",
    "cache_write",
    "content_hash",
]
