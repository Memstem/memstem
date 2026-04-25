# Changelog

All notable changes to Memstem will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repo scaffold (README, ARCHITECTURE, ROADMAP)
- Architecture Decision Records (ADRs 0001-0006)
- Source skeleton for `memstem` package (core, adapters, hygiene, servers)
- Frontmatter specification and MCP API specification
- CI workflow, issue/PR templates, contributing guide
- MIT license, security policy
- `memstem.core.frontmatter`: typed `Frontmatter` model, `parse`, `serialize`,
  and `validate` helpers conforming to `docs/frontmatter-spec.md`
- `memstem.core.storage`: `Vault` class with `read`, `write`, `walk`, `delete`;
  typed `Memory` model wrapping frontmatter + body + vault-relative path
- `memstem.core.embeddings`: `OllamaEmbedder` HTTP client (uses `/api/embed`)
  with single + batch methods, paragraph-aware `chunk_text` helper, and a
  `requires_ollama` pytest marker registered for integration tests
- `memstem.core.index`: SQLite + FTS5 + sqlite-vec hybrid index with
  versioned migrations, `upsert` / `upsert_vectors` / `delete`, and
  `query_fts` / `query_vec` returning typed `FtsHit` / `VecHit` records;
  cascading deletes for tags/links/vectors and a wikilink extractor
- `memstem.core.search`: `Search` orchestrator for hybrid retrieval —
  Reciprocal Rank Fusion over BM25 + vector hits, materializing typed
  `Result` records (memory + score + per-source ranks) from the vault.
  Sanitizes FTS5-special characters from natural-language queries; falls
  back to BM25-only if the embedder errors so the daemon never goes mute
- `memstem.adapters.openclaw`: `OpenClawAdapter` reads Ari/OpenClaw
  markdown files (memory, daily logs, skills) into normalized
  `MemoryRecord` objects. Reconcile walks paths once; watch streams
  records via `watchdog` inotify. Classifies files by name (`SKILL.md`,
  `YYYY-MM-DD.md`, else memory) and falls back to filename/H1 for titles
  when frontmatter is absent
- `memstem.adapters.claude_code`: `ClaudeCodeAdapter` reads Claude Code
  session JSONL files into one `MemoryRecord` per session (type=session).
  Body is the concatenated user/assistant transcript with tool blocks
  summarized (`[tool_use: Bash]`, `[tool_result]`) so it stays readable.
  Title falls back from `ai-title` → first user prompt → session UUID.
  Re-emits the full session on file change; pipeline upserts by `ref`
- `memstem.servers.mcp_server`: `build_server(vault, index, embedder=None)`
  factory returning a `FastMCP` instance with five tools matching the
  spec in `docs/mcp-api.md`: `memstem_search`, `memstem_get`,
  `memstem_list_skills`, `memstem_get_skill`, `memstem_upsert`. Auto-
  generates vault paths on upsert when none is supplied (memories /
  skills / sessions / daily layouts)
- `memstem.core.pipeline`: `Pipeline` converts adapter-emitted
  `MemoryRecord` objects into canonical `Memory` writes — stable id per
  `(source, ref)`, vault write, index upsert, embed-and-store chunks
- CLI commands (`memstem init|daemon|search|reindex|mcp`) wired up via
  Typer. `init` scaffolds a vault and `_meta/config.yaml`; `daemon`
  runs OpenClaw + Claude Code adapters into the pipeline (reconcile +
  watch); `search` and `reindex` operate on the local vault; `mcp`
  serves the FastMCP tools on stdio for Claude Code et al.
- `memstem.migrate` + `scripts/migrate-from-flipclaw.py`: one-shot
  migration that walks `~/ari/memory/`, `~/ari/skills/`, and recent
  Claude Code sessions, tags every record with `flipclaw-migration`,
  and runs them through the standard pipeline. Default is dry-run
  (counts + sample preview); `--apply` writes
- Multi-agent OpenClaw support: `OpenClawWorkspace(path, tag)`,
  `OpenClawAdapterConfig(agent_workspaces, shared_files)`,
  `ClaudeCodeAdapterConfig(project_roots, extra_files)`, all wired
  through `Config.adapters`. The adapter walks per-agent
  `MEMORY.md` / `CLAUDE.md` / `memory/*.md` / `skills/*/SKILL.md`,
  tagging records with `agent:<tag>` (plus `core` for MEMORY.md and
  `instructions` for CLAUDE.md). Shared files (e.g. HARD-RULES.md)
  emit with a `shared` tag instead. Legacy paths-only mode preserved
  for back-compat
- `scripts/install.sh`: one-line installer for an unattended install
  (`curl ... | bash -s -- --yes`). Verifies Python 3.11+, installs
  pipx and memstem, optionally installs Ollama and pulls
  `nomic-embed-text`, scaffolds the vault, runs `memstem doctor` to
  confirm. `--no-ollama`, `--no-model`, `--vault`, `--from-git` knobs
- `memstem doctor`: CLI command that verifies Python version, vault +
  config existence, index health, embedder reachability, and every
  configured adapter target (OpenClaw workspaces / shared files,
  Claude Code roots / extras). Exits non-zero if any check fails
- ADR 0007: remote-machine ingestion is out of scope until Phase 3+;
  documented sync-and-watch as the recommended workaround

### Changed

- `Adapter.watch` and `Adapter.reconcile` are declared without `async`
  in the ABC so subclass async generators type-check cleanly

### PR #16 additions (setup wizard)

- `memstem.discovery`: auto-discovery helpers for OpenClaw agent
  workspaces (`~/*/openclaw.json`), shared rules files (`HARD-RULES.md`),
  Claude Code session roots (`~/.claude/projects`), and per-user
  Claude Code instructions (`~/.claude/CLAUDE.md`). Each candidate
  carries a content count so the installer can highlight non-empty
  agents.
- `memstem init` setup wizard: defaults to interactive per-candidate
  prompts; `-y` / `--non-interactive` auto-includes every candidate
  with content. `--home <path>` lets tests and headless installs scope
  the discovery to a sandbox.

### PR #17 additions (Claude Code extras)

- `ClaudeCodeAdapter` now accepts `extra_files`. Each is read as a
  markdown instructions file and emitted as a record with the
  `instructions` tag (type=memory). Reconcile yields them alongside
  session JSONLs; watch picks up changes via the parent dir.
- CLI daemon constructs the Claude Code adapter from
  `cfg.adapters.claude_code.extra_files` and lists the watched extras
  in its startup banner.
