"""Configuration loading and defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class EmbeddingConfig(BaseModel):
    """Embedding model configuration.

    Memstem ships four backends; pick one via ``provider`` and supply
    only that one's fields. API keys live in environment variables
    named by ``api_key_env`` (never written to the vault):

    - ``ollama`` (default) — local. Requires no API key. ``base_url``
      defaults to ``http://localhost:11434``.
    - ``openai`` — OpenAI or any OpenAI-compatible endpoint (Together,
      Mistral, Groq, vLLM, LM Studio, ...). Set ``base_url`` to the
      provider's URL when not using OpenAI directly.
    - ``gemini`` — Google's Generative Language API.
    - ``voyage`` — Voyage AI (Anthropic's embedding partner).
    """

    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str | None = None
    """Override the provider's default base URL. Defaults to ``http://localhost:11434``
    for ollama; provider-specific defaults for openai/gemini/voyage."""

    dimensions: int = 768

    api_key_env: str | None = None
    """Name of the environment variable holding the API key. Defaults
    to ``OPENAI_API_KEY`` / ``GOOGLE_API_KEY`` / ``VOYAGE_API_KEY``
    depending on provider; ignored for ollama."""

    workers: int = 2
    """Concurrent embedding workers draining the queue. CPU-bound Ollama
    is happiest at 1; API providers tolerate higher values (4 is a
    sensible cap to avoid hitting per-account rate limits)."""

    batch_size: int = 8
    """How many records the worker pulls from the queue per iteration.
    Each record's chunks are batched in a single API call when the
    backend supports it."""


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


class OpenClawLayout(BaseModel):
    """Per-workspace path conventions.

    All fields are relative to the workspace root and default to the
    canonical OpenClaw layout (`MEMORY.md`, `CLAUDE.md`, `memory/`,
    `skills/`). Override any of them to point the adapter at a workspace
    with a non-standard memory layout — e.g. memory under
    `notes/` instead of `memory/`, or skills disabled entirely.
    """

    memory_md: str | None = "MEMORY.md"
    """Always-loaded core file path (relative to workspace). Set to
    ``None`` to skip — useful for workspaces that don't follow the
    MEMORY.md convention."""

    claude_md: str | None = "CLAUDE.md"
    """Per-agent operational rules file path. ``None`` to skip."""

    memory_dirs: list[str] = Field(default_factory=lambda: ["memory"])
    """Directories whose ``*.md`` descendants get ingested as memories.
    Each directory is walked recursively. Empty list = no recursive
    memory ingestion (only top-level MEMORY.md / CLAUDE.md)."""

    skills_dirs: list[str] = Field(default_factory=lambda: ["skills"])
    """Directories whose ``**/SKILL.md`` descendants get ingested as
    skills. Empty list = no skill ingestion."""


class OpenClawWorkspace(BaseModel):
    """One OpenClaw agent workspace and its display tag.

    Records emitted from this workspace get an `agent:<tag>` tag so callers
    can filter or group results per agent. Override ``layout`` to point at
    a workspace with a non-standard memory layout.
    """

    path: Path
    tag: str
    layout: OpenClawLayout = Field(default_factory=OpenClawLayout)


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
