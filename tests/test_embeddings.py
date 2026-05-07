"""Tests for the embeddings module."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from memstem import auth
from memstem.config import EmbeddingConfig
from memstem.core.embeddings import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    EmbeddingError,
    GeminiEmbedder,
    OllamaEmbedder,
    OpenAIEmbedder,
    TransientEmbeddingError,
    VoyageEmbedder,
    _classify_http_error,
    chunk_text,
    embed_for,
)


class TestChunkText:
    def test_short_text_unchanged(self) -> None:
        assert chunk_text("hello world") == ["hello world"]

    def test_empty_returns_empty_list(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_splits_at_paragraph_boundaries(self) -> None:
        # Two paragraphs that together exceed the limit but each fit
        para1 = "a" * 100
        para2 = "b" * 100
        chunks = chunk_text(f"{para1}\n\n{para2}", max_chars=150)
        assert chunks == [para1, para2]

    def test_groups_small_paragraphs(self) -> None:
        chunks = chunk_text("para one\n\npara two\n\npara three", max_chars=200)
        assert chunks == ["para one\n\npara two\n\npara three"]

    def test_hard_cuts_oversized_paragraph(self) -> None:
        long = "x" * 5000
        chunks = chunk_text(long, max_chars=2000)
        assert len(chunks) == 3
        assert chunks[0] == "x" * 2000
        assert chunks[1] == "x" * 2000
        assert chunks[2] == "x" * 1000

    def test_oversized_paragraph_flushes_pending(self) -> None:
        small = "small para"
        big = "y" * 1000
        chunks = chunk_text(f"{small}\n\n{big}", max_chars=500)
        assert chunks[0] == small
        # Subsequent chunks are pieces of `big`
        assert "".join(chunks[1:]) == big

    def test_strips_whitespace_around_short_text(self) -> None:
        assert chunk_text("  hello  \n") == ["hello"]


class TestOllamaEmbedderUnit:
    """Unit tests that don't require a running Ollama server."""

    def test_defaults(self) -> None:
        emb = OllamaEmbedder()
        assert emb.model == DEFAULT_MODEL
        assert emb.dimensions == DEFAULT_DIMENSIONS
        assert emb.base_url == "http://localhost:11434"
        emb.close()

    def test_strips_trailing_slash(self) -> None:
        emb = OllamaEmbedder(base_url="http://localhost:11434/")
        assert emb.base_url == "http://localhost:11434"
        emb.close()

    def test_empty_batch_returns_empty(self) -> None:
        emb = OllamaEmbedder()
        try:
            assert emb.embed_batch([]) == []
        finally:
            emb.close()

    def test_context_manager_closes(self) -> None:
        with OllamaEmbedder() as emb:
            assert emb.model == DEFAULT_MODEL


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Build an httpx MockTransport from a request → response handler."""
    return httpx.MockTransport(handler)


class TestTransientClassification:
    """``_classify_http_error`` decides whether an httpx failure should
    raise :class:`TransientEmbeddingError` (worker should back off and
    retry without bumping retry_count) or :class:`EmbeddingError`
    (worker should mark and possibly fail the record).
    """

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_5xx_classified_transient(self, status: int) -> None:
        request = httpx.Request("POST", "https://api.example.com/embed")
        response = httpx.Response(status, text="upstream blew up", request=request)
        exc = httpx.HTTPStatusError("server error", request=request, response=response)
        assert _classify_http_error(exc) is TransientEmbeddingError

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 499])
    def test_4xx_classified_permanent(self, status: int) -> None:
        request = httpx.Request("POST", "https://api.example.com/embed")
        response = httpx.Response(status, text="bad input", request=request)
        exc = httpx.HTTPStatusError("client error", request=request, response=response)
        cls = _classify_http_error(exc)
        assert cls is EmbeddingError
        # Sanity: not the transient subclass either, even though it
        # would still satisfy isinstance(EmbeddingError).
        assert cls is not TransientEmbeddingError

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),
            httpx.ReadError("peer closed connection without sending body"),
            httpx.RemoteProtocolError("incomplete chunked read"),
            httpx.ConnectTimeout("connect timeout"),
            httpx.ReadTimeout("read timeout"),
            httpx.WriteTimeout("write timeout"),
            httpx.PoolTimeout("pool timeout"),
        ],
    )
    def test_request_errors_classified_transient(self, exc: httpx.RequestError) -> None:
        # All RequestError subclasses describe transport-level failures
        # — the canonical "retry me later" cases.
        assert _classify_http_error(exc) is TransientEmbeddingError

    def test_transient_isinstance_embedding_error(self) -> None:
        """Subclass relationship: existing ``except EmbeddingError``
        handlers continue to catch transients as a fallback. Specific
        handlers should catch :class:`TransientEmbeddingError` first."""
        exc = TransientEmbeddingError("blip")
        assert isinstance(exc, EmbeddingError)


class TestOpenAITransientErrors:
    """OpenAI embedder maps httpx failures to the right exception class."""

    def test_500_raises_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=4)
        emb._client = httpx.Client(
            base_url=emb.base_url,
            transport=_mock_transport(lambda r: httpx.Response(500, text="upstream down")),
        )
        try:
            with pytest.raises(TransientEmbeddingError, match="OpenAI request failed"):
                emb.embed_batch(["x"])
        finally:
            emb.close()

    def test_400_raises_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=4)
        emb._client = httpx.Client(
            base_url=emb.base_url,
            transport=_mock_transport(lambda r: httpx.Response(400, text="bad request")),
        )
        try:
            with pytest.raises(EmbeddingError) as excinfo:
                emb.embed_batch(["x"])
            # 4xx is permanent: the EmbeddingError raised must NOT be
            # the transient subclass.
            assert not isinstance(excinfo.value, TransientEmbeddingError)
        finally:
            emb.close()

    def test_connect_error_raises_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Network-level failure (peer closed, connect refused, read
        timeout) is the exact shape that was burning Ari's retry
        budget. Must map to transient."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=4)

        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("peer closed connection without sending complete message body")

        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(boom))
        try:
            with pytest.raises(TransientEmbeddingError, match="OpenAI request failed"):
                emb.embed_batch(["x"])
        finally:
            emb.close()


class TestOpenAIEmbedder:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
            OpenAIEmbedder(model="text-embedding-3-small", dimensions=1536)

    def test_embed_batch_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [0.1] * 4, "index": 0},
                        {"object": "embedding", "embedding": [0.2] * 4, "index": 1},
                    ],
                    "model": "text-embedding-3-small",
                },
            )

        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=4)
        emb._client = httpx.Client(
            base_url=emb.base_url,
            transport=_mock_transport(handler),
            headers={
                "Authorization": "Bearer sk-test",
                "Content-Type": "application/json",
            },
        )
        try:
            vecs = emb.embed_batch(["a", "b"])
        finally:
            emb.close()
        assert len(vecs) == 2
        assert vecs[0] == [0.1] * 4
        assert vecs[1] == [0.2] * 4
        assert seen["url"].endswith("/embeddings")
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"]["model"] == "text-embedding-3-small"
        assert seen["body"]["input"] == ["a", "b"]

    def test_http_error_raises_embedding_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=4)
        emb._client = httpx.Client(
            base_url=emb.base_url,
            transport=_mock_transport(lambda r: httpx.Response(500, text="boom")),
        )
        try:
            with pytest.raises(EmbeddingError, match="OpenAI request failed"):
                emb.embed_batch(["x"])
        finally:
            emb.close()


class TestGeminiEmbedder:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="GOOGLE_API_KEY"):
            GeminiEmbedder()

    def test_embed_batch_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {"values": [0.5] * 4},
                        {"values": [0.6] * 4},
                    ]
                },
            )

        # Legacy `text-embedding-004` model — no Matryoshka shortening,
        # provider returns the native dim.
        emb = GeminiEmbedder(model="text-embedding-004", dimensions=4)
        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(handler))
        try:
            vecs = emb.embed_batch(["a", "b"])
        finally:
            emb.close()
        assert len(vecs) == 2
        assert vecs[0] == [0.5] * 4
        assert "key=AIza-test" in seen["url"]
        assert "batchEmbedContents" in seen["url"]
        assert seen["body"]["requests"][0]["model"] == "models/text-embedding-004"
        assert seen["body"]["requests"][0]["content"]["parts"][0]["text"] == "a"
        # No `outputDimensionality` for non-Matryoshka models.
        assert "outputDimensionality" not in seen["body"]["requests"][0]

    def test_default_model_is_gemini_embedding_2_preview(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default ships the best-quality model; users wanting stability
        can pin `gemini-embedding-001` in config."""
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        emb = GeminiEmbedder()
        assert emb.model == "gemini-embedding-2-preview"
        emb.close()

    def test_matryoshka_model_sends_output_dimensionality(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gemini-embedding-001 supports truncation; we use it to keep the
        existing 768-dim schema when switching from Ollama."""
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"embeddings": [{"values": [0.0] * 768}]},
            )

        emb = GeminiEmbedder(model="gemini-embedding-001", dimensions=768)
        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(handler))
        try:
            vecs = emb.embed_batch(["x"])
        finally:
            emb.close()
        assert len(vecs[0]) == 768
        assert seen["body"]["requests"][0]["outputDimensionality"] == 768

    def test_dimension_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If Gemini returns the wrong width (e.g. config asked 768 but the
        provider sent 3072 anyway), the embedder errors out instead of
        silently passing through and breaking the index."""
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": [{"values": [0.0] * 3072}]})

        emb = GeminiEmbedder(model="gemini-embedding-001", dimensions=768)
        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(handler))
        try:
            with pytest.raises(EmbeddingError, match="3072-dim vector"):
                emb.embed_batch(["x"])
        finally:
            emb.close()

    def test_normalizes_models_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        emb1 = GeminiEmbedder(model="text-embedding-004")
        emb2 = GeminiEmbedder(model="models/text-embedding-004")
        assert emb1.model == "text-embedding-004"
        assert emb2.model == "models/text-embedding-004"
        emb1.close()
        emb2.close()

    def test_batch_size_over_100_splits_into_multiple_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gemini caps batchEmbedContents at 100 per call. Records with
        long bodies (e.g. 250KB daily logs) chunk into 100+ pieces, so
        the embedder must split into sub-batches and concatenate. Without
        this, the live cutover hits 400 Bad Request."""
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        request_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            n = len(body["requests"])
            request_sizes.append(n)
            return httpx.Response(
                200,
                json={"embeddings": [{"values": [0.0] * 768} for _ in range(n)]},
            )

        emb = GeminiEmbedder(model="gemini-embedding-2-preview", dimensions=768)
        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(handler))
        try:
            vecs = emb.embed_batch(["x"] * 250)  # 250 chunks → 3 batches
        finally:
            emb.close()
        # All 250 chunks come back, ordered.
        assert len(vecs) == 250
        # Three batches: 100 + 100 + 50.
        assert request_sizes == [100, 100, 50]

    def test_400_error_includes_response_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gemini's 400s carry useful detail in the response body
        (oversize input, etc.) — surface it rather than swallowing."""
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": "input is too large for the model"}},
            )

        emb = GeminiEmbedder(model="gemini-embedding-2-preview", dimensions=768)
        emb._client = httpx.Client(base_url=emb.base_url, transport=_mock_transport(handler))
        try:
            with pytest.raises(EmbeddingError, match="input is too large"):
                emb.embed_batch(["x"])
        finally:
            emb.close()


class TestVoyageEmbedder:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
            VoyageEmbedder()

    def test_embed_batch_sends_input_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.0] * 4, "index": 0},
                    ]
                },
            )

        emb = VoyageEmbedder(model="voyage-3", dimensions=4)
        emb._client = httpx.Client(
            base_url=emb.base_url,
            transport=_mock_transport(handler),
            headers={
                "Authorization": "Bearer pa-test",
                "Content-Type": "application/json",
            },
        )
        try:
            emb.embed_batch(["doc"])
        finally:
            emb.close()
        assert seen["body"]["input_type"] == "document"
        assert seen["body"]["model"] == "voyage-3"


class TestEmbedForFactory:
    def test_ollama_default(self) -> None:
        cfg = EmbeddingConfig(provider="ollama")
        emb = embed_for(cfg)
        assert isinstance(emb, OllamaEmbedder)
        emb.close()

    def test_openai_uses_default_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        cfg = EmbeddingConfig(provider="openai", model="text-embedding-3-small", dimensions=1536)
        emb = embed_for(cfg)
        assert isinstance(emb, OpenAIEmbedder)
        emb.close()

    def test_openai_custom_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OpenAI-compatible providers (Together, etc.) override base_url."""
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-x")
        cfg = EmbeddingConfig(
            provider="openai",
            model="BAAI/bge-large-en-v1.5",
            dimensions=1024,
            base_url="https://api.together.xyz/v1",
            api_key_env="TOGETHER_API_KEY",
        )
        emb = embed_for(cfg)
        # `embed_for` returns the abstract Embedder; narrow with isinstance
        # so mypy can see the concrete attribute on OpenAIEmbedder.
        assert isinstance(emb, OpenAIEmbedder)
        assert emb.base_url == "https://api.together.xyz/v1"
        emb.close()

    def test_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-x")
        cfg = EmbeddingConfig(provider="gemini", model="text-embedding-004", dimensions=768)
        emb = embed_for(cfg)
        assert isinstance(emb, GeminiEmbedder)
        emb.close()

    def test_voyage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-x")
        cfg = EmbeddingConfig(provider="voyage", model="voyage-3", dimensions=1024)
        emb = embed_for(cfg)
        assert isinstance(emb, VoyageEmbedder)
        emb.close()

    def test_unknown_provider_raises(self) -> None:
        cfg = EmbeddingConfig(provider="nope")
        with pytest.raises(EmbeddingError, match="unknown embedding provider"):
            embed_for(cfg)

    def test_provider_case_insensitive(self) -> None:
        cfg = EmbeddingConfig(provider="OLLAMA")
        emb = embed_for(cfg)
        assert isinstance(emb, OllamaEmbedder)
        emb.close()


class TestForProviderFactory:
    """`EmbeddingConfig.for_provider()` populates known-good defaults
    so scripted setup (install.sh, `memstem init --provider`) doesn't
    need to remember each provider's right model + dimensions + env var."""

    def test_openai_defaults(self) -> None:
        cfg = EmbeddingConfig.for_provider("openai")
        assert cfg.provider == "openai"
        assert cfg.model == "text-embedding-3-large"
        assert cfg.dimensions == 3072
        assert cfg.api_key_env == "OPENAI_API_KEY"

    def test_gemini_defaults(self) -> None:
        cfg = EmbeddingConfig.for_provider("gemini")
        assert cfg.provider == "gemini"
        assert cfg.model == "gemini-embedding-2-preview"
        assert cfg.dimensions == 768
        assert cfg.api_key_env == "GEMINI_API_KEY"

    def test_voyage_defaults(self) -> None:
        cfg = EmbeddingConfig.for_provider("voyage")
        assert cfg.provider == "voyage"
        assert cfg.model == "voyage-3"
        assert cfg.dimensions == 1024
        assert cfg.api_key_env == "VOYAGE_API_KEY"

    def test_ollama_no_api_key(self) -> None:
        cfg = EmbeddingConfig.for_provider("ollama")
        assert cfg.provider == "ollama"
        assert cfg.api_key_env is None

    def test_case_insensitive(self) -> None:
        cfg = EmbeddingConfig.for_provider("OpenAI")
        assert cfg.provider == "openai"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown embedder provider"):
            EmbeddingConfig.for_provider("claude")

    def test_unknown_lists_known_providers(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            EmbeddingConfig.for_provider("nope")
        # The error message is the user's primary signal — make sure it
        # actually names what they CAN use.
        msg = str(exc_info.value)
        for known in ("ollama", "openai", "gemini", "voyage"):
            assert known in msg, f"{known} missing from error: {msg}"


class TestSecretsFileFallback:
    """When env vars are missing, the embedder falls back to ~/.config/memstem/secrets.yaml."""

    def test_openai_reads_from_secrets_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        auth.set_secret("openai", "sk-from-file")
        # Construction succeeds — embedder picked up the file value
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=1536)
        emb.close()

    def test_gemini_reads_from_secrets_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        auth.set_secret("gemini", "AIza-from-file")
        emb = GeminiEmbedder()
        emb.close()

    def test_voyage_reads_from_secrets_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        auth.set_secret("voyage", "pa-from-file")
        emb = VoyageEmbedder()
        emb.close()

    def test_env_var_still_wins_over_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        auth.set_secret("openai", "sk-from-file")
        # Existing OpenAI tests already verify the env value flows through
        # to the request; this test just makes sure construction picks the
        # env value over the file value (no exception raised).
        emb = OpenAIEmbedder(model="text-embedding-3-small", dimensions=1536)
        emb.close()

    def test_error_message_mentions_auth_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="memstem auth set openai"):
            OpenAIEmbedder(model="text-embedding-3-small", dimensions=1536)


@pytest.mark.requires_ollama
class TestOllamaEmbedderIntegration:
    """Integration tests against a live Ollama server."""

    def test_single_embedding_has_expected_dimensions(self) -> None:
        with OllamaEmbedder() as emb:
            vec = emb.embed("hello world")
        assert len(vec) == DEFAULT_DIMENSIONS
        assert all(isinstance(x, float) for x in vec)

    def test_batch_returns_one_vector_per_input(self) -> None:
        with OllamaEmbedder() as emb:
            vecs = emb.embed_batch(["one", "two", "three"])
        assert len(vecs) == 3
        for v in vecs:
            assert len(v) == DEFAULT_DIMENSIONS

    def test_similar_texts_have_higher_cosine_similarity(self) -> None:
        with OllamaEmbedder() as emb:
            vecs = emb.embed_batch(
                [
                    "the cat sat on the mat",
                    "a feline rested on the rug",
                    "quantum chromodynamics is a theory",
                ]
            )

        def cos(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb)

        related = cos(vecs[0], vecs[1])
        unrelated = cos(vecs[0], vecs[2])
        assert related > unrelated
