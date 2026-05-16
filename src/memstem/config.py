"""Configuration loading and defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

PROVIDER_PROFILES: dict[str, dict[str, object]] = {
    "ollama": {
        "model": "nomic-embed-text",
        "dimensions": 768,
        "api_key_env": None,
    },
    "openai": {
        "model": "text-embedding-3-large",
        "dimensions": 3072,
        "api_key_env": "OPENAI_API_KEY",
    },
    "gemini": {
        "model": "gemini-embedding-2-preview",
        "dimensions": 768,
        "api_key_env": "GEMINI_API_KEY",
    },
    "voyage": {
        "model": "voyage-3",
        "dimensions": 1024,
        "api_key_env": "VOYAGE_API_KEY",
    },
}
"""Known-good defaults for each shipped provider. Used by
``EmbeddingConfig.for_provider()`` to populate a fresh config without
the caller having to remember the right model + dimensions + env var
combination. Override any field via the constructor as usual."""


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

    Use :meth:`for_provider` to build a config with sensible per-provider
    defaults (model, dimensions, ``api_key_env``).
    """

    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str | None = None
    """Override the provider's default base URL. Defaults to ``http://localhost:11434``
    for ollama; provider-specific defaults for openai/gemini/voyage."""

    dimensions: int = 768

    api_key_env: str | None = None
    """Name of the environment variable holding the API key. Defaults
    to ``OPENAI_API_KEY`` / ``GEMINI_API_KEY`` / ``VOYAGE_API_KEY``
    depending on provider; ignored for ollama."""

    workers: int = 2
    """Concurrent embedding workers draining the queue. CPU-bound Ollama
    is happiest at 1; API providers tolerate higher values (4 is a
    sensible cap to avoid hitting per-account rate limits)."""

    batch_size: int = 8
    """How many records the worker pulls from the queue per iteration.
    Each record's chunks are batched in a single API call when the
    backend supports it."""

    @classmethod
    def for_provider(cls, provider: str) -> EmbeddingConfig:
        """Build a config pre-populated with sensible defaults for ``provider``.

        Raises :class:`ValueError` if the provider isn't in
        :data:`PROVIDER_PROFILES`. Use this when scripted setup needs
        a working config without remembering each provider's right
        model + dimensions + env var combination.
        """
        provider_lc = provider.lower()
        if provider_lc not in PROVIDER_PROFILES:
            known = ", ".join(sorted(PROVIDER_PROFILES))
            raise ValueError(f"unknown embedder provider {provider!r}. Known: {known}")
        profile = PROVIDER_PROFILES[provider_lc]
        return cls(provider=provider_lc, **profile)  # type: ignore[arg-type]


DEFAULT_TYPE_BIAS: dict[str, float] = {
    "distillation": 1.10,
    "memory": 1.05,
    "skill": 1.05,
    "project": 1.05,
    "decision": 1.05,
    "person": 1.0,
    "daily": 1.0,
    "session": 0.85,
}
"""Default per-type ranking multiplier applied after RRF + importance.

The intent is to make default search clearly prefer **curated and derived**
records (distillations, extracted memories, skills, project records) over
**raw** records (conversational sessions). Values are intentionally bounded
in ``[0.85, 1.10]`` — small enough that a clearly-better raw match still
wins on relevance, large enough to break ties in favour of derived content.

Operators tune this via ``search.type_bias`` in ``_meta/config.yaml``;
unspecified types fall back to ``1.0`` (neutral). Setting every type to
``1.0`` recovers the pre-bias behaviour exactly."""


class SearchConfig(BaseModel):
    """Hybrid search configuration."""

    rrf_k: int = 60
    bm25_weight: float = 1.0
    vector_weight: float = 1.0
    default_limit: int = 10
    importance_weight: float = 0.2
    """ADR 0008 Tier 1 alpha. ``final = rrf * (1 + alpha * importance)``.

    ``0.0`` disables the boost entirely (RRF order is final, matching v0.1
    behavior). ``0.2`` is the default — importance acts as a tiebreaker
    rather than a forcing function. Crank it higher (e.g. ``0.5``) to make
    pinned/curated memories outrank conversational noise more aggressively;
    drop it (``0.05``) to keep raw retrieval relevance dominant. The
    documented bound is ``0.0`` to ``1.0``; values outside cause a noisy
    boost without a clear meaning."""

    type_bias: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_TYPE_BIAS))
    """Per-type multiplier applied after the importance boost.

    The full final score is::

        final = rrf * (1 + importance_weight * importance) * type_bias[type]

    Unlisted types default to ``1.0`` (neutral). This is the policy knob
    that makes "prefer distillations and curated memories over raw
    sessions" explicit and tunable. To disable it entirely, set every
    type to ``1.0`` (or supply an empty mapping)."""


class HygieneConfig(BaseModel):
    """Hygiene worker configuration."""

    dedup_threshold: float = 0.95
    decay_half_life_days: int = 90
    skill_extraction_enabled: bool = True
    query_log_enabled: bool = True
    """ADR 0008 Tier 1 query log (search/get exposure log).

    Records every search-result exposure and every ``memstem_get`` open
    into the bounded ``query_log`` table inside ``_meta/index.db``. The
    hygiene worker reads this log to bump ``importance`` on records the
    user actually retrieved. Set to ``False`` to disable logging
    entirely — useful for shared-host setups where the query text is
    sensitive."""

    query_log_max_rows: int = 100_000
    """Row cap for the ``query_log`` table. When exceeded, the oldest
    rows are pruned by id to keep the table bounded between hygiene
    sweeps. 100k is roughly 30 days at 100 queries/day with 30 hits
    each. Lower this on storage-constrained hosts; raise it for vaults
    that run hygiene infrequently."""


class HttpServerConfig(BaseModel):
    """Local HTTP server configuration.

    The daemon co-hosts a small HTTP API on loopback so first-party clients
    (CLI tools, editor extensions, future first-party UIs) can call into
    the same `Search` / `Vault` / `Index` instances the watch loop uses,
    without spawning a per-query subprocess.

    Loopback-only by design — there's no auth surface in v0.1.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7821


class McpServerConfig(BaseModel):
    """MCP server configuration.

    Each Claude Code session that uses Memstem spawns its own
    ``memstem mcp`` subprocess. Without an idle timeout these
    subprocesses linger after the parent session ends — they pile up
    over weeks until they contend on the SQLite file lock and embed
    workers start crashing on "database is locked".

    ``idle_timeout_seconds`` causes the MCP process to self-terminate
    after the configured number of seconds with no tool calls. Claude
    Code transparently respawns it on the next request, so users never
    see the interruption. Set to ``0`` to disable (useful for tests
    and for users who run MCP from scripts that should outlive idle
    periods).
    """

    idle_timeout_seconds: int = 1800  # 30 minutes


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

    session_dirs: list[str] = Field(default_factory=list)
    """Directories whose ``*.trajectory.jsonl`` descendants get ingested
    as session records. Empty by default — opt in by listing the
    directories where the agent runtime writes full conversation
    trajectories (e.g. ``["agents/main/sessions"]`` for OpenClaw's
    standard layout). Each trajectory becomes one ``type:session``
    record containing the chronological transcript of user prompts and
    assistant responses."""

    extra_files: list[str] = Field(default_factory=list)
    """Additional top-level files (relative to workspace root) to ingest
    as memory records. Each gets the workspace's ``agent:<tag>`` tag,
    same as ``MEMORY.md``/``CLAUDE.md``. Use this for per-agent system
    files beyond the two-file convention — e.g. ``SOUL.md``, ``USER.md``,
    ``AGENTS.md``. Auto-discovery is intentionally NOT done: workspaces
    often hold dated snapshots and append-only logs that would churn
    the index, so the operator enumerates what's worth indexing."""


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


class CodexAdapterConfig(BaseModel):
    """Configuration for the Codex (OpenAI) adapter.

    See ADR 0022. Each root is optional; a missing root is silently
    skipped, so this adapter is safe to enable by default on hosts
    without Codex installed.
    """

    codex_home: Path | None = None
    """Root of the Codex install (defaults to ``~/.codex``).
    The three roots below default to ``<codex_home>/{sessions,skills,memories}``
    when unset, mirroring the standard Codex layout."""

    sessions_root: Path | None = None
    """Override for the JSONL sessions directory."""

    skills_root: Path | None = None
    """Override for the user-skills directory.
    ``<skills_root>/.system/`` is always skipped (vendor-shipped skills)."""

    memories_root: Path | None = None
    """Override for the user-memories directory."""

    ingest_sessions: bool = True
    ingest_skills: bool = True
    ingest_memories: bool = True


class AdaptersConfig(BaseModel):
    """Per-adapter configuration block."""

    openclaw: OpenClawAdapterConfig = Field(default_factory=OpenClawAdapterConfig)
    claude_code: ClaudeCodeAdapterConfig = Field(default_factory=ClaudeCodeAdapterConfig)
    codex: CodexAdapterConfig = Field(default_factory=CodexAdapterConfig)


class Config(BaseModel):
    """Top-level Memstem configuration."""

    vault_path: Path
    index_path: Path | None = None  # defaults to <vault>/_meta/index.db
    embedding: EmbeddingConfig = EmbeddingConfig()
    search: SearchConfig = SearchConfig()
    hygiene: HygieneConfig = HygieneConfig()
    http: HttpServerConfig = Field(default_factory=HttpServerConfig)
    mcp: McpServerConfig = Field(default_factory=McpServerConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
