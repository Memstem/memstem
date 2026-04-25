"""Tests for the embeddings module."""

from __future__ import annotations

import pytest

from memstem.core.embeddings import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    OllamaEmbedder,
    chunk_text,
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
