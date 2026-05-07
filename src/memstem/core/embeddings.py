"""Embedder backends and a paragraph-aware chunker.

Memstem supports four embedding backends out of the box:

- :class:`OllamaEmbedder` — local, the default. No API key, runs against
  any Ollama daemon (default ``http://localhost:11434``).
- :class:`OpenAIEmbedder` — `api.openai.com` and any OpenAI-compatible
  endpoint (Together, Mistral, Groq, vLLM, LM Studio, etc.) via the
  ``base_url`` knob.
- :class:`GeminiEmbedder` — Google's `generativelanguage.googleapis.com`
  REST endpoint, e.g. ``text-embedding-004``.
- :class:`VoyageEmbedder` — `api.voyageai.com`, Anthropic's recommended
  partner. ``voyage-3`` tops common retrieval benchmarks.

All four implement the :class:`Embedder` interface (`embed`, `embed_batch`,
`close`). ``embed_for(config)`` is the factory that turns an
:class:`~memstem.config.EmbeddingConfig` into the right backend. API keys
are read from environment variables named in the config; nothing secret
ever lands in the vault.

Long memories are split into chunks at paragraph boundaries before
embedding; the index stores one vector per chunk so a long document can
match a query that touches only one of its sections.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import httpx

if TYPE_CHECKING:
    from memstem.config import EmbeddingConfig

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_DIMENSIONS = 768
DEFAULT_TIMEOUT = 120.0
"""Per-request timeout. Set generously because nomic-embed-text on CPU
can spend tens of seconds per chunk under bulk-ingest load (the daemon
queues many requests during a `migrate --apply` of a fresh vault).
Tighter timeouts during steady-state operation can be set via
`EmbeddingConfig.timeout` once that knob is wired through (v0.2)."""

DEFAULT_CHUNK_CHARS = 2048


class EmbeddingError(Exception):
    """Raised when an embedding call fails permanently.

    ``EmbeddingError`` covers failures the worker should treat as
    final for the current attempt: invalid input (4xx), unexpected
    response shape, configuration errors. The worker increments the
    record's ``retry_count`` and eventually gives up.

    Use :class:`TransientEmbeddingError` for failures that are likely
    to clear on their own (network blips, timeouts, 5xx).
    """


class TransientEmbeddingError(EmbeddingError):
    """Raised when an embedding call fails for a transient reason.

    Network errors, read timeouts, partial response bodies, and 5xx
    responses go here. The worker treats these specially: it does not
    bump the record's ``retry_count`` (so a 30-second OpenAI hiccup
    doesn't burn through ``max_retries`` for every record in flight)
    and it backs off before its next tick. A run of consecutive
    transients still triggers exponential backoff; a permanent
    embedder problem is detected because ``mark_embed_error`` is never
    called and a separate consecutive-transient counter eventually
    surfaces in logs.

    Subclasses :class:`EmbeddingError` so existing ``except
    EmbeddingError`` handlers still catch it as a fallback. Specific
    handlers should catch :class:`TransientEmbeddingError` first.
    """


def _classify_http_error(exc: httpx.HTTPError) -> type[EmbeddingError]:
    """Pick the right exception class for an httpx failure.

    Transient (network blips, server-side problems): return
    :class:`TransientEmbeddingError`. Permanent (4xx client errors,
    other unrecognised httpx failures): return :class:`EmbeddingError`.
    The caller still constructs the message and ``raise ... from exc``.

    httpx exception hierarchy reference: ``httpx.HTTPError`` is the
    base for both ``RequestError`` (network/transport, no response)
    and ``HTTPStatusError`` (got a response, status was 4xx/5xx).
    """
    # 5xx server errors: transient. 4xx client errors: permanent (the
    # request will keep failing the same way until the input changes).
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        if response is not None and 500 <= response.status_code < 600:
            return TransientEmbeddingError
        return EmbeddingError
    # All RequestError subclasses (TimeoutException, ConnectError,
    # ReadError, RemoteProtocolError, etc.) describe transport-level
    # failures — the request never got a clean response. These are the
    # canonical "retry me later" cases.
    if isinstance(exc, httpx.RequestError):
        return TransientEmbeddingError
    # Unknown HTTPError subclass: be conservative and treat as
    # permanent so we don't loop forever on a misclassified bug.
    return EmbeddingError


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split text into chunks no longer than `max_chars`, preferring paragraph breaks.

    A paragraph that exceeds the limit is hard-cut on character boundaries.
    Empty input returns an empty list.
    """
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text.strip()]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chars:
            chunks.append(current)
            current = para
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _read_api_key(env_var: str, provider: str) -> str:
    # Env var first, then ~/.config/memstem/secrets.yaml so cron, PM2, and
    # headless shells don't need their own export. The auth module owns
    # both lookups.
    from memstem.auth import get_secret

    key = get_secret(provider.lower(), env_var=env_var)
    if not key:
        raise EmbeddingError(
            f"{provider} embedder needs an API key. "
            f"Either export ${env_var}, run "
            f"`memstem auth set {provider.lower()} <key>`, or change "
            f"`embedding.provider` in _meta/config.yaml."
        )
    return key


class Embedder(ABC):
    """Common interface for every embedding backend.

    Subclasses must implement :meth:`embed_batch` (the rest is provided).
    Implementations are sync — the queue worker handles concurrency by
    running multiple embedders in parallel async tasks rather than
    making each backend juggle its own httpx async client.
    """

    dimensions: int

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Implementations should send them in
        a single API call when the provider supports it; fall back to
        sequential calls otherwise. Must raise :class:`EmbeddingError`
        on any failure so the queue worker can retry."""

    def close(self) -> None:  # noqa: B027 — default no-op; subclasses override if needed
        """Release any HTTP/network resources. Default: no-op."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class OllamaEmbedder(Embedder):
    """HTTP client for Ollama's `/api/embed` endpoint."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.post(
                "/api/embed",
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc)(f"Ollama request failed: {exc}") from exc

        data = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError(f"unexpected /api/embed response: {data}")
        return [list(map(float, vec)) for vec in embeddings]

    def close(self) -> None:
        self._client.close()


class OpenAIEmbedder(Embedder):
    """OpenAI-compatible embedder.

    Talks to ``{base_url}/embeddings`` with the standard OpenAI shape
    (``model`` + ``input``). The default ``base_url`` is OpenAI itself,
    but any compatible provider works — set ``base_url`` to e.g.
    ``https://api.together.xyz/v1`` for Together, ``http://localhost:1234/v1``
    for LM Studio, etc.
    """

    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"

    # OpenAI's `/embeddings` endpoint accepts up to 2048 inputs per call,
    # but the per-request token cap (~300k) is the real constraint for
    # large records. A single 1.5 MB memory chunked at 2048 chars yields
    # ~750 chunks ≈ 380k tokens — over the cap. Splitting into batches
    # of 100 keeps every request well under both limits and matches the
    # Gemini embedder's batching.
    MAX_BATCH_SIZE = 100

    def __init__(
        self,
        model: str,
        dimensions: int,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        api_key = _read_api_key(api_key_env, "OpenAI")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            sub = texts[start : start + self.MAX_BATCH_SIZE]
            results.extend(self._embed_one_batch(sub))
        return results

    def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                "/embeddings",
                json={"model": self.model, "input": texts, "encoding_format": "float"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Bubble the response body up — OpenAI's 400s carry useful
            # detail (oversize input, invalid token, etc.) that the bare
            # HTTP status line hides.
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise _classify_http_error(exc)(
                f"OpenAI request failed: {exc} — body: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc)(f"OpenAI request failed: {exc}") from exc

        data = response.json()
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingError(f"unexpected OpenAI response: {data}")
        # Items are ordered by `index`; sort defensively in case the
        # provider doesn't guarantee it.
        ordered = sorted(items, key=lambda e: e.get("index", 0))
        return [list(map(float, e["embedding"])) for e in ordered]

    def close(self) -> None:
        self._client.close()


class GeminiEmbedder(Embedder):
    """Google Generative Language API embedder.

    Targets ``generativelanguage.googleapis.com`` with the
    ``:batchEmbedContents`` endpoint so a list of chunks costs one HTTP
    round-trip.

    Default model is ``gemini-embedding-2-preview`` — the current
    best-quality Gemini embedding model (~20% recall improvement on
    heterogeneous corpora over ``gemini-embedding-001``, 8k context
    window vs 2k, multimodal-capable). Its native dimension is 3072,
    but it supports Matryoshka representation: requesting a smaller
    ``dimensions`` value (e.g. 768 or 1536) returns a truncated vector
    that's still meaningful and well-aligned with the original. This
    lets users keep a vault that was set up with a 768-dim Ollama
    schema and switch to Gemini without rebuilding the index — the
    embedder sends ``outputDimensionality`` automatically based on the
    configured ``dimensions`` so vectors land at exactly the right
    width.

    The "preview" label means Google may change behavior or deprecate
    the model. Users who want maximum stability can pin
    ``model: gemini-embedding-001`` (the previous-generation
    production-stable model) instead. Both are listed in
    ``_MATRYOSHKA_MODELS`` so dimension truncation works for either.

    Older legacy model names (``text-embedding-004``, ``embedding-001``)
    are still accepted via config but Google has deprecated them on
    most API keys; pick a current model.
    """

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
    DEFAULT_API_KEY_ENV = "GOOGLE_API_KEY"
    DEFAULT_MODEL = "gemini-embedding-2-preview"

    # Gemini's `batchEmbedContents` caps requests at 100 items per call
    # (per Google's API docs). Records with bigger bodies (long daily
    # logs, multi-turn session transcripts) chunk into more pieces, so
    # we split into batches and concatenate.
    MAX_BATCH_SIZE = 100

    # Native widths (no Matryoshka). Used to decide whether to send
    # `outputDimensionality` — only for models that support it.
    _MATRYOSHKA_MODELS = frozenset(
        {
            "gemini-embedding-001",
            "models/gemini-embedding-001",
            "gemini-embedding-2",
            "models/gemini-embedding-2",
            "gemini-embedding-2-preview",
            "models/gemini-embedding-2-preview",
        }
    )

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        dimensions: int = 768,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self._api_key = _read_api_key(api_key_env, "Gemini")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    def _supports_matryoshka(self) -> bool:
        return self.model in self._MATRYOSHKA_MODELS

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Gemini's `batchEmbedContents` caps at 100 items per call.
        # Records with very long bodies (daily logs, session
        # transcripts) chunk into more pieces, so we issue multiple
        # requests and concatenate.
        results: list[list[float]] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            sub = texts[start : start + self.MAX_BATCH_SIZE]
            results.extend(self._embed_one_batch(sub))
        return results

    def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        # Gemini's model field uses the ``models/<name>`` form.
        full_model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        request_template: dict[str, Any] = {
            "model": full_model,
        }
        if self._supports_matryoshka():
            # Truncate the native 3072-dim vector to the configured width.
            # Models without Matryoshka ignore this field; a few legacy
            # models error on it, hence the gating above.
            request_template["outputDimensionality"] = self.dimensions
        body = {
            "requests": [{**request_template, "content": {"parts": [{"text": t}]}} for t in texts]
        }
        try:
            response = self._client.post(
                f"/{full_model}:batchEmbedContents",
                params={"key": self._api_key},
                json=body,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Bubble the response body up — Gemini's 400s carry useful
            # detail (oversize input, invalid token, etc.) that the
            # bare HTTP status line hides.
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise _classify_http_error(exc)(
                f"Gemini request failed: {exc} — body: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc)(f"Gemini request failed: {exc}") from exc

        data = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError(f"unexpected Gemini response: {data}")
        vecs = [list(map(float, e["values"])) for e in embeddings]
        # Sanity check: provider returned the dim we asked for.
        for v in vecs:
            if len(v) != self.dimensions:
                raise EmbeddingError(
                    f"Gemini returned {len(v)}-dim vector but config "
                    f"requested {self.dimensions}. Set "
                    f"`embedding.dimensions: {len(v)}` or pick a Matryoshka "
                    f"model (gemini-embedding-001)."
                )
        return vecs

    def close(self) -> None:
        self._client.close()


class VoyageEmbedder(Embedder):
    """Voyage AI embedder.

    Anthropic's recommended embedding partner. Targets
    ``api.voyageai.com/v1/embeddings`` with a shape similar to OpenAI's
    plus a Voyage-specific ``input_type`` flag (``document`` for indexing,
    ``query`` for retrieval — we always index, so we use ``document``).
    """

    DEFAULT_BASE_URL = "https://api.voyageai.com/v1"
    DEFAULT_API_KEY_ENV = "VOYAGE_API_KEY"

    def __init__(
        self,
        model: str = "voyage-3",
        dimensions: int = 1024,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        api_key = _read_api_key(api_key_env, "Voyage")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    # Voyage's `/embeddings` accepts up to 128 inputs per call (per
    # docs); chunk-heavy records can exceed that, so we batch.
    MAX_BATCH_SIZE = 128

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            sub = texts[start : start + self.MAX_BATCH_SIZE]
            results.extend(self._embed_one_batch(sub))
        return results

    def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                "/embeddings",
                json={"model": self.model, "input": texts, "input_type": "document"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise _classify_http_error(exc)(
                f"Voyage request failed: {exc} — body: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc)(f"Voyage request failed: {exc}") from exc

        data = response.json()
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingError(f"unexpected Voyage response: {data}")
        ordered = sorted(items, key=lambda e: e.get("index", 0))
        return [list(map(float, e["embedding"])) for e in ordered]

    def close(self) -> None:
        self._client.close()


def embed_for(config: EmbeddingConfig) -> Embedder:
    """Factory: build the right :class:`Embedder` for an
    :class:`~memstem.config.EmbeddingConfig`.

    Raises :class:`EmbeddingError` if the provider is unknown or its
    required API key is missing.
    """
    provider = config.provider.lower()
    if provider == "ollama":
        return OllamaEmbedder(
            model=config.model,
            base_url=config.base_url or DEFAULT_BASE_URL,
            dimensions=config.dimensions,
        )
    if provider == "openai":
        return OpenAIEmbedder(
            model=config.model,
            dimensions=config.dimensions,
            api_key_env=config.api_key_env or OpenAIEmbedder.DEFAULT_API_KEY_ENV,
            base_url=config.base_url or OpenAIEmbedder.DEFAULT_BASE_URL,
        )
    if provider == "gemini":
        return GeminiEmbedder(
            model=config.model,
            dimensions=config.dimensions,
            api_key_env=config.api_key_env or GeminiEmbedder.DEFAULT_API_KEY_ENV,
            base_url=config.base_url or GeminiEmbedder.DEFAULT_BASE_URL,
        )
    if provider == "voyage":
        return VoyageEmbedder(
            model=config.model,
            dimensions=config.dimensions,
            api_key_env=config.api_key_env or VoyageEmbedder.DEFAULT_API_KEY_ENV,
            base_url=config.base_url or VoyageEmbedder.DEFAULT_BASE_URL,
        )
    raise EmbeddingError(
        f"unknown embedding provider: {config.provider!r}. "
        "Supported: ollama, openai, gemini, voyage."
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_DIMENSIONS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "Embedder",
    "EmbeddingError",
    "GeminiEmbedder",
    "OllamaEmbedder",
    "OpenAIEmbedder",
    "TransientEmbeddingError",
    "VoyageEmbedder",
    "chunk_text",
    "embed_for",
]
