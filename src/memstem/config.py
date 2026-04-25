"""Configuration loading and defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


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


class OpenClawWorkspace(BaseModel):
    """One OpenClaw agent workspace and its display tag.

    Records emitted from this workspace get an `agent:<tag>` tag so callers
    can filter or group results per agent.
    """

    path: Path
    tag: str


class OpenClawAdapterConfig(BaseModel):
    """Configuration for the OpenClaw adapter."""

    agent_workspaces: list[OpenClawWorkspace] = Field(default_factory=list)
    """Per-agent workspaces. Each `<path>/MEMORY.md`, `CLAUDE.md`,
    `memory/*.md`, and `skills/*/SKILL.md` becomes a record tagged with
    `agent:<tag>`."""

    shared_files: list[Path] = Field(default_factory=list)
    """Agent-agnostic files (e.g. `~/ari/HARD-RULES.md`). Emitted with a
    `shared` tag instead of an `agent:*` tag."""


class ClaudeCodeAdapterConfig(BaseModel):
    """Configuration for the Claude Code adapter."""

    project_roots: list[Path] = Field(default_factory=list)
    """Roots under which to find session JSONL files (recursively)."""

    extra_files: list[Path] = Field(default_factory=list)
    """Additional CLAUDE.md or instructions files to ingest as memories."""


class AdaptersConfig(BaseModel):
    """Per-adapter configuration block."""

    openclaw: OpenClawAdapterConfig = Field(default_factory=OpenClawAdapterConfig)
    claude_code: ClaudeCodeAdapterConfig = Field(default_factory=ClaudeCodeAdapterConfig)


class Config(BaseModel):
    """Top-level Memstem configuration."""

    vault_path: Path
    index_path: Path | None = None  # defaults to <vault>/_meta/index.db
    embedding: EmbeddingConfig = EmbeddingConfig()
    search: SearchConfig = SearchConfig()
    hygiene: HygieneConfig = HygieneConfig()
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
