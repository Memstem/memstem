"""Configuration loading and defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class EmbeddingConfig(BaseModel):
    """Embedding model configuration."""

    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str = "http://localhost:11434"
    dimensions: int = 768


class SearchConfig(BaseModel):
    """Hybrid search configuration."""

    rrf_k: int = 60
    bm25_weight: float = 1.0
    vector_weight: float = 1.0
    default_limit: int = 10


class HygieneConfig(BaseModel):
    """Hygiene worker configuration."""

    dedup_threshold: float = 0.95
    decay_half_life_days: int = 90
    skill_extraction_enabled: bool = True


class Config(BaseModel):
    """Top-level Memstem configuration."""

    vault_path: Path
    index_path: Path | None = None  # defaults to <vault>/_meta/index.db
    embedding: EmbeddingConfig = EmbeddingConfig()
    search: SearchConfig = SearchConfig()
    hygiene: HygieneConfig = HygieneConfig()
