"""Embedding backend (Ollama by default) and a paragraph-aware chunker.

Long memories are split into chunks at paragraph boundaries before embedding;
the index stores one vector per chunk so that a long document can match a
query that touches only one of its sections.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import httpx

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_DIMENSIONS = 768
DEFAULT_TIMEOUT = 30.0
DEFAULT_CHUNK_CHARS = 2048


class EmbeddingError(Exception):
    """Raised when an embedding call fails."""


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


class OllamaEmbedder:
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

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

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
            raise EmbeddingError(f"Ollama request failed: {exc}") from exc

        data = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError(f"unexpected /api/embed response: {data}")
        return [list(map(float, vec)) for vec in embeddings]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
