# Changelog

All notable changes to Memstem will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — per-workspace layout overrides for OpenClaw

- **`OpenClawWorkspace` now accepts a `layout` field** specifying which
  paths inside the workspace get ingested. Lets toolkit users with
  non-canonical OpenClaw layouts (memories under `notes/` instead of
  `memory/`, skills disabled, custom `MEMORY.md` filename) point the
  adapter at their actual paths instead of forking the adapter or
  symlinking files.
- New `OpenClawLayout` model with four configurable fields, all with
  defaults that preserve current behavior:
  - `memory_md` — top-level core file (default `MEMORY.md`; `None` to skip).
  - `claude_md` — operational rules file (default `CLAUDE.md`; `None` to skip).
  - `memory_dirs` — list of directories whose `*.md` descendants are
    ingested (default `["memory"]`; empty list = no recursive ingestion).
  - `skills_dirs` — list of directories whose `**/SKILL.md` descendants
    are ingested (default `["skills"]`; empty list = no skills).
- Both reconcile (`_iter_workspace_files`) and watch
  (`_classify_workspace_path`) honor the layout, so live file events
  flow into the index using the same path conventions configured for
  reconcile.
- Existing configs are unchanged: omitting `layout` falls back to the
  canonical defaults via Pydantic's `default_factory`. 6 new tests cover
  the override paths (custom memory dir, skip MEMORY.md, skip skills,
  multiple memory dirs, default unchanged, watch classifier).

## [0.4.0] — 2026-04-26

Two related cutover fixes shipped together: the post-restart re-embed
storm (PR #30) and the MCP-spawned-child-has-no-API-key silent
BM25-only fallback (PR #31). Plus the `__init__.py` version string
catches up with `pyproject.toml` after drifting since 0.1.0. CI matrix
also updated to `actions/checkout@v6`, `actions/setup-python@v6`, and
`codecov/codecov-action@v6` via three dependabot bumps.

### Fixed — skip re-embed when content unchanged (PR #30, schema v3)

- **Pipeline no longer re-enqueues a record whose body and embedder
  signature haven't changed.** Earlier versions enqueued every record
  on every emit — so a `pm2 restart memstem` re-embedded all ~765
  records via the reconcile pass, even when no body had changed.
  Wasteful in time and (for API providers) in rate-limit quota.
  Schema v3 adds an `embed_state` table tracking the body hash +
  embedder signature each memory was last successfully embedded
  with; the pipeline checks this via the new `Index.needs_reembed`
  helper before enqueueing and skips when hash + signature both
  match. The worker writes a fresh `embed_state` row after every
  successful vector upsert. Net result: post-restart reconcile is a
  no-op for unchanged records.
- **Re-upserting a memory no longer cascade-deletes its child rows.**
  `Index.upsert` was using `INSERT OR REPLACE INTO memories`, which
  SQLite implements as DELETE-then-INSERT and so triggered
  `ON DELETE CASCADE` on `embed_state` and `embed_queue`. The
  practical effect was that the worker's hard-won "embedded" record
  evaporated on the next reconcile. Switched to `INSERT ... ON
  CONFLICT(id) DO UPDATE` so the row stays in place and child
  references survive.
- **Schema migration v3** is automatic on first connect; legacy
  databases get an `embed_state` row backfilled for every memory
  that already has vectors, with `embed_signature = NULL`. NULL is
  treated as "compatible with any signature" by `needs_reembed` so
  the upgrade doesn't trigger a global re-embed — the first time a
  body actually changes (or a user runs `memstem reindex`), the
  legacy NULL gets stamped with the real signature.
- 23 new tests covering the embed-state helpers, the pipeline skip
  path (unchanged body, changed body, signature change, no vectors
  yet), the worker's stamp-on-success behavior, and the v3 backfill
  (populates for vectorized memories, skips empty ones, doesn't
  clobber existing rows).

### Fixed — connect-clients propagates embedder API key into MCP env (PR #31)

- **The MCP entries written by `connect-clients` now include the
  embedder's API key.** Earlier versions wrote `"env": {}` for Claude
  Code and no `env` block at all for OpenClaw, so when those clients
  spawned `memstem mcp` as a subprocess, the child got no API key —
  the parent shell's env doesn't propagate to MCP children. The
  result was a silent fallback to BM25-only search: vectors were in
  the index, but every `memstem_search` result came back with
  `vec_rank: null` because `_maybe_embedder()` caught the
  `EmbeddingError` and built `Search(embedder=None)`.
- New `mcp_env_from_embedding(api_key_env)` helper in
  `integration.py` reads the configured `embedding.api_key_env` and
  resolves it against the install-time shell, returning a dict
  suitable for the MCP entry's `env` block. Empty for local
  providers (Ollama) and for missing/blank env vars.
- `register_mcp_server` and `register_openclaw_mcp_server` now accept
  an `env: dict[str, str] | None` kwarg that merges into the written
  entry's env block. Defensive-copy semantics — never mutates the
  module-level `DEFAULT_*_ENTRY` constants.
- `memstem connect-clients` resolves the API key once up front and
  threads it into both registration paths. Prints a one-line warning
  if the configured `api_key_env` is set in config but missing from
  the install shell, telling the user to export it and re-run.
- 13 new tests covering the helper (set/missing/blank/None/os.environ
  fallback), the Claude Code register path (env merges, default
  preserved when env=None, no mutation of constants, custom-entry
  + env compose), and the OpenClaw register path (env adds an
  otherwise-absent block, empty/None env preserves no-block default,
  no mutation).

### Fixed — version string mismatch

- `src/memstem/__init__.py` had been pinned to `__version__ = "0.1.0"`
  since the original 0.1.0 release in PR #22, while `pyproject.toml`
  was bumped to 0.2.0 (cdc4088) and 0.3.0 (000384b) without the
  matching `__init__.py` change. Now both files agree on `0.4.0`.
  Future release commits should bump both in the same diff.

### Changed — CI dependencies (dependabot PRs #1, #2, #3)

- `actions/setup-python` 5 → 6
- `codecov/codecov-action` 4 → 6
- `actions/checkout` 4 → 6

## [0.3.0] — 2026-04-26

### Added

- **`connect-clients` now registers Memstem MCP in each OpenClaw
  agent's `openclaw.json`.** Earlier versions only patched the
  agent's CLAUDE.md with the "use Memstem MCP first" directive — but
  if the agent's openclaw.json didn't have a `mcp.servers.memstem`
  entry, the directive was unhonorable: the agent looked for the
  MCP, didn't find it, and fell back to grep or CLI. Same shape of
  bug as the v0.2.0 Claude Code MCP-location fix, now closed for
  OpenClaw too.
- New `register_openclaw_mcp_server` helper in `integration.py` —
  reads/writes the agent's `mcp.servers.<name>` block while
  preserving every other key in the (large) `openclaw.json`. Same
  Change return type, same `.bak`, same dry-run semantics as
  `register_mcp_server`. Direct JSON edit (rather than shelling out
  to `openclaw mcp set`) keeps `integration.py` filesystem-only.
- New `openclaw_config_for_workspace` resolver — mirror of
  `claude_md_targets_for_openclaw` for the agent's OpenClaw config.
  Accepts a workspace dir, a CLAUDE.md path, or the openclaw.json
  itself.
- New `DEFAULT_OPENCLAW_MCP_SERVER_ENTRY` constant (`{command, args}`
  shape — OpenClaw's `mcp.servers` doesn't use Claude Code's `type`
  discriminator).
- 16 new tests covering the registration helper (entry shapes,
  preservation of other servers, idempotency, .bak, dry-run, missing
  files, malformed JSON, custom-entry override) and the workspace
  resolver (workspace dir / direct file / sibling lookup / missing
  cases).

## [0.2.0] — 2026-04-26

Cumulative release covering PRs #23–#29 plus the MCP location fix.
Shipped features: complete installer toolkit (`install.sh` +
`memstem doctor`), four pluggable embedder backends (Ollama / OpenAI /
Gemini / Voyage) with an always-on embed queue, Gemini default
`gemini-embedding-2-preview` with Matryoshka dimensions, thread-safe
SQLite Index, batch-size-aware Gemini calls, and the cutover
`connect-clients` registration moved to the location current Claude
Code releases actually read.

### Fixed — connect-clients MCP location

- **`connect-clients` was registering Memstem in a config file Claude
  Code no longer reads.** Earlier versions wrote the
  `mcpServers.memstem` entry to `~/.claude/settings.json`, but current
  Claude Code releases discover MCP servers from `~/.claude.json`
  (the file `claude mcp add` manages). The settings.json block was
  silently inert, so no Claude session — interactive or
  relay-spawned — actually picked up the Memstem MCP server. Sessions
  fell back to the `memstem` CLI via `Bash`, which works but skips
  the direct MCP path.
- `register_mcp_server` now writes the new entry shape (`type`,
  `command`, `args`, `env`) and `connect-clients` defaults to
  `~/.claude.json`. A new `remove_legacy_mcp_server` step cleans up
  the stale entry from `~/.claude/settings.json` automatically (with
  a `.bak`); `--legacy-settings PATH` overrides the location for
  testing.
- Six new tests covering the cleanup helper (file missing, entry
  absent, entry present alongside others, lone entry that empties
  the `mcpServers` key, dry-run, invalid JSON).

### Fixed (PR #29)

- **Gemini batch size limit.** `batchEmbedContents` caps requests at
  100 items per call; records with long bodies (~250KB daily logs)
  chunk into 100+ pieces and were hitting `400 Bad Request` on the
  live cutover. `GeminiEmbedder.embed_batch` now splits oversize
  inputs into sub-batches of `MAX_BATCH_SIZE` (=100) and
  concatenates results — same outward contract, multiple HTTP calls
  under the hood.
- **400 errors include the response body.** Gemini's error
  messages live in the JSON body and explain *why* (input too large,
  bad model, etc.). The bare HTTP status line was hiding them.
  `EmbeddingError` now surfaces the first 500 chars of the body.
- Two new tests covering the batch split (250 chunks → 3 calls of
  100/100/50) and the surfaced-error format.

### Fixed (PR #28)

- **Concurrent SQLite access from the embed worker.** `Index.connect()`
  opens with `check_same_thread=False` so the worker can run sync
  SQLite calls under `asyncio.to_thread`, but Python's `sqlite3`
  module isn't actually thread-safe on a single connection
  (concurrent commits race; the sqlite-vec extension keeps thread-
  local state). Added a `threading.RLock` around every Index read
  and write path. Workers can still run concurrently — the lock is
  cheap and only held during the SQLite call, not the embedder
  HTTP call.
- Two new pounding tests in `TestThreadSafety` confirm 16-way
  concurrent upserts + queue ops complete without `cannot commit -
  no transaction is active` or `bad parameter or other API misuse`
  errors. Without the lock, those errors hit within ~10 ops.

### Changed (PR #27)

- `GeminiEmbedder` default model is now `gemini-embedding-2-preview`
  (current best-quality Gemini embedding: ~20% recall improvement on
  heterogeneous corpora vs `gemini-embedding-001`, 8k context
  window, multimodal-capable). Google retired `text-embedding-004`
  (the previous default shipped in PR #26); the same API key works
  for the new model. Users who want maximum stability over absolute
  quality can pin `model: gemini-embedding-001` in
  `_meta/config.yaml` — that's the production-stable predecessor.
- `GeminiEmbedder` sends `outputDimensionality` for models that
  support Matryoshka representation (`gemini-embedding-001`,
  `gemini-embedding-2`, `gemini-embedding-2-preview`). This lets
  users keep an existing 768-dim Ollama schema and switch to Gemini
  without rebuilding the index — Gemini's native 3072d gets
  truncated server-side to whatever `dimensions` is configured.
- Gemini response width is validated against config; mismatches
  raise a clear `EmbeddingError` rather than silently corrupting
  the index.

### Added (PR #26)

- **Pluggable embedder backends** via a formal `Embedder` ABC and an
  `embed_for(EmbeddingConfig)` factory. Four implementations ship:
  `OllamaEmbedder` (default, local), `OpenAIEmbedder` (with
  `base_url` knob for OpenAI-compatible providers like Together,
  Mistral, Groq, vLLM, LM Studio), `GeminiEmbedder`
  (`text-embedding-004` — same 768d as Ollama, no reindex on switch),
  and `VoyageEmbedder` (Anthropic's recommended partner). API keys
  live in env vars named by `EmbeddingConfig.api_key_env`; nothing
  secret lands in the vault.
- **Always-on embed queue.** New `embed_queue` SQLite table (schema
  v2). The pipeline writes records synchronously and enqueues each
  one for embedding. `EmbedWorker` drains the queue with retry +
  backoff; failed records land in `failed=1` after `max_retries`
  (default 5) and surface in `memstem doctor`. The daemon runs the
  worker continuously alongside reconcile + watch; one-shot drains
  via `memstem embed`.
- **`memstem embed` CLI command** for manual queue drains.
  `--retry-failed` resets records that hit max retries.
- `EmbeddingConfig.workers` (default 2) and `batch_size` (default 8)
  tune queue throughput; CPU Ollama at 1, API providers at 4+.
- ADR 0009 documents the rationale and the architecture.

### Changed (PR #26)

- `pipeline.process` no longer embeds inline; ingest latency is now
  bounded by disk + SQLite, not by the embedder. The previous
  inline-embed path is gone.
- `memstem doctor` reports `Embed queue: N pending, M failed` so
  operators can see whether the queue is keeping up.
- `memstem doctor`'s embedder check now works for every provider
  (was Ollama-only).
- `memstem migrate --no-embed` and `install.sh --migrate-no-embed`
  are kept as no-op aliases for back-compat with PR #23/#24
  invocations — embedding is always deferred now.
- Schema migration tracker no longer accumulates extra rows on each
  migration; `schema_version` keeps exactly one row at the latest
  applied version.
- `Index.connect()` opens the SQLite connection with
  `check_same_thread=False` so the embed worker can run sync SQLite
  calls under `asyncio.to_thread`. Writes are still serialized by
  SQLite's single-writer lock.

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
