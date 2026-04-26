# Changelog

All notable changes to Memstem will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (PR #25)

- **Path collisions across agents.** Daily logs and skills with the
  same title/date from different agents collapsed into one record on
  disk. On Brad's box, 326 daily files reduced to 80, and ~130
  OpenClaw records were silently lost during the first cutover.
  Pipeline now extracts `agent:<tag>` from record tags and produces
  agent-scoped paths: `daily/<agent>/<date>.md`, `skills/<agent>/<slug>.md`,
  `memories/<source>/<agent>/<id>.md`. Records without an agent tag
  keep the legacy paths, so MCP-driven upserts and the FlipClaw
  migration's tag-less ingest path are unchanged.
- **Orphan rows in the index when paths rotate.** `Index.upsert` now
  detects when another row already holds the target `path` under a
  different id and cleans up that row's tags/links/FTS/vec entries
  before inserting the new one. Previously these orphans accumulated
  in the FTS5 table and surfaced as "hit X missing from memories
  table" warnings during search.

### Added

- `memstem migrate` is now a top-level CLI command (was previously
  only reachable via `scripts/migrate-from-flipclaw.py`). Same flags:
  `--apply`, `--days`, `--vault`, `--openclaw`, `--claude-root`,
  plus new `--no-embed` and `--progress-every`. The script wrapper
  still works unchanged.
- `memstem migrate --no-embed` skips vector embedding during the bulk
  import. Records still land in vault + FTS5; run `memstem reindex`
  later to backfill vectors. This is the practical answer for
  CPU-only Ollama where bulk embedding queues up tens of seconds
  per chunk and saturates the runner.
- `memstem migrate --progress-every N` prints a heartbeat every N
  records during `--apply` (default 25, 0 to silence).
- `install.sh --migrate` runs `memstem migrate --apply` after init so
  a fresh box ends up with history imported.
- `install.sh --migrate-days N` overrides the Claude Code session
  lookback window (default 30). Smaller values cut the embed load on
  fresh installs — older sessions can land via the daemon's watch
  loop over time.
- `install.sh --migrate-no-embed` passes `--no-embed` through to
  `memstem migrate`. The recommended pattern for a fresh install on
  CPU-only Ollama: `--migrate --migrate-no-embed --start-daemon`,
  then run `memstem reindex` overnight to backfill vectors.
- `install.sh --start-daemon` starts `memstem daemon` under PM2 (no-op
  with a warning if PM2 isn't installed). Combined with
  `--connect-clients`, the installer is a single-shot cutover.
- Ollama service health check in `install.sh`: after install, polls
  `http://localhost:11434/api/tags` until the daemon responds (up to
  30s). On macOS, attempts `brew services start ollama` first.
- `install.sh --connect-clients` now prints a dry-run diff before
  applying, so the operator sees what's about to change.
- Smoke tests for `install.sh` (`tests/test_install_sh.py`): `bash -n`
  syntax check, `--help` flag-coverage check, unknown-flag rejection.

### Changed

- Default `OllamaEmbedder` timeout bumped from 30s → 120s. The 30s
  default was too tight under bulk-ingest load: a fresh `migrate
  --apply` queues many large chunks against a CPU-only runner, and
  individual embed calls were timing out before they ever reached
  the head of the queue. 120s is generous in steady state and
  recoverable in bulk.

### Fixed

- `install.sh --yes` now propagates `-y` to `memstem init`, so an
  unattended install no longer hangs at the setup wizard's per-agent
  prompts.

## [0.1.0] - 2026-04-XX

First tagged release. Phase 1 v0.1 — running on the live EC2 box,
ingesting from Claude Code + multi-agent OpenClaw, exposing MCP
search, with FlipClaw retired. Tag date is filled in at release
time after Brad validates the cutover.

### Added

- Initial repo scaffold (README, ARCHITECTURE, ROADMAP)
- Architecture Decision Records (ADRs 0001-0008)
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
  confirm. `--no-ollama`, `--no-model`, `--vault`, `--from-git`,
  `--connect-clients`, `--remove-flipclaw` knobs
- `memstem doctor`: CLI command that verifies Python version, vault +
  config existence, index health, embedder reachability, and every
  configured adapter target (OpenClaw workspaces / shared files,
  Claude Code roots / extras). Exits non-zero if any check fails
- `memstem.discovery`: auto-discovery helpers for OpenClaw agent
  workspaces (`~/*/openclaw.json`), shared rules files (`HARD-RULES.md`),
  Claude Code session roots (`~/.claude/projects`), and per-user
  Claude Code instructions (`~/.claude/CLAUDE.md`). Each candidate
  carries a content count so the installer can highlight non-empty
  agents
- `memstem init` setup wizard: defaults to interactive per-candidate
  prompts; `-y` / `--non-interactive` auto-includes every candidate
  with content. `--home <path>` lets tests and headless installs scope
  the discovery to a sandbox
- `ClaudeCodeAdapter` accepts `extra_files`. Each is read as a
  markdown instructions file and emitted as a record with the
  `instructions` tag (type=memory). Reconcile yields them alongside
  session JSONLs; watch picks up changes via the parent dir
- `memstem.integration`: idempotent wiring of Memstem into client
  config. `register_mcp_server` adds a `mcpServers.memstem` block to a
  Claude Code `settings.json` (preserving other servers).
  `apply_directive` inserts or updates a versioned
  `<!-- memstem:directive v1 -->` block in a CLAUDE.md, leaving
  surrounding content untouched. `remove_flipclaw_hook` strips the
  legacy `claude-code-bridge.py` SessionEnd hook. Each edit writes a
  `.bak` and supports `dry_run` to preview a unified diff
- `memstem connect-clients` CLI command wraps the above. Defaults patch
  `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, and the CLAUDE.md
  in every workspace from the vault config. `--openclaw <path>` is
  repeatable; `--remove-flipclaw` disables the legacy bridge;
  `--dry-run` previews; `--settings` and `--claude-md` override paths
  for tests and non-default installs
- ADR 0007: remote-machine ingestion is out of scope until Phase 3+;
  documented sync-and-watch as the recommended workaround
- ADR 0008: tiered-memory design (importance scoring, distillations,
  hygiene worker) for v0.2. Status proposed; no code lands until Brad
  reviews

### Changed

- `Adapter.watch` and `Adapter.reconcile` are declared without `async`
  in the ABC so subclass async generators type-check cleanly
- CI test matrix runs Linux at full strictness; macOS and Windows are
  marked experimental for visibility-only
