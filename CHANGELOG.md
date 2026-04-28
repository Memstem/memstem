# Changelog

All notable changes to Memstem will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — `memstem auth` for persistent embedder API keys (#41)

- **New command group `memstem auth set/show/remove`** persists API keys
  to `~/.config/memstem/secrets.yaml` (mode 0600, gitignore-irrelevant
  because it lives outside any vault). When the corresponding env var is
  not exported in the current shell, the embedder factory falls back to
  this file — so cron jobs, PM2 ecosystems, systemd units, and headless
  servers all work without each one needing its own export.
- **Resolution order:** the explicitly configured `embedding.api_key_env`
  wins; if unset, the provider's default (`OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `VOYAGE_API_KEY`); if still empty, the secrets file.
- **Why this matters:** previously, `memstem search` from a regular shell
  silently degraded to lexical-only when the env var was missing — same
  vault, same config, but worse results, with no obvious signal. The MCP
  server worked fine because Claude Code passed the key via its own env
  block. Issue #41 has the full reproduction; tactical CLI now lets users
  set the key once per machine instead of per-shell.
- **Test override:** `MEMSTEM_SECRETS_FILE` env var redirects the file
  path (used by the test suite for hermetic isolation; the global pytest
  fixture in `tests/conftest.py` points every test at a tmp path).
- 49 new tests — `tests/test_auth.py` (32 covering the module),
  `tests/test_cli.py::TestAuth` (12 covering the CLI), and
  `tests/test_embeddings.py::TestSecretsFileFallback` (5 covering the
  embedder fallback path).

### Added — Claude Code / OpenClaw search skill

- **First-party `memstem-search` skill** under `clients/skills/memstem-search/`.
  A single `SKILL.md` with frontmatter compatible with both Claude Code's skill
  loader and OpenClaw's bundled-skill format (`metadata.clawdbot` and
  `metadata.openclaw` namespaces side by side). Installed by symlink or copy
  into the consumer's skill directory.
- **Why a skill in addition to the MCP:** Claude Code does not pre-load MCP
  tool schemas; they appear as deferred tools and must be loaded via
  `ToolSearch` before they can be called. Agents miss this step and skip
  MemStem even when configured. A skill is pre-listed in the session-start
  available-skills block, so the agent sees `memstem-search` immediately
  with no schema-loading dance.
- **Skill owns the full priority ladder.** The procedure tries
  MCP → HTTP `/search` (the daemon shipped above) → `memstem` CLI →
  grep, in order. Callers do not need to remember the order; invoking
  the skill is enough.
- Distribution to consumers stays manual in this PR (symlink/copy from
  `clients/skills/memstem-search/` into `~/.claude/skills/`,
  `<project>/.claude/skills/`, or `~/<openclaw-workspace>/skills/`).
  Automated install via `memstem connect-clients` lands in a follow-up.

### Added — local HTTP API + Obsidian plugin scaffold

- **`memstem daemon` now co-hosts a local HTTP server** on
  `127.0.0.1:7821` (configurable via `http.port` in
  `_meta/config.yaml`). The server reuses the daemon's live `Vault`,
  `Index`, and `Embedder` instances — no per-query subprocess, no
  duplicate state. Endpoints mirror the MCP tool list one-to-one:
  `GET /health`, `GET /version`, `POST /search`, `GET /memory/{id_or_path}`.
  Loopback-only by design; v0.1 has no auth surface.
- **First-party Obsidian plugin scaffold under `clients/obsidian/`.**
  v0.1 proves the integration loop end-to-end: connects to the
  daemon's `/health`, shows the connection state in the Obsidian
  status bar, and exposes a settings tab for daemon URL + poll
  interval. Search modal, sidebar pane, and "New memory" command
  arrive in follow-up PRs.
- **ADR 0010** documents the four design decisions: monorepo,
  co-hosted HTTP, BRAT-first distribution, read+write semantics with
  frontmatter scaffolding.
- **New deps:** `fastapi>=0.110.0`, `uvicorn>=0.30.0`. Imported lazily
  inside the daemon path so the CLI's other commands don't pay for
  them.
- 14 new tests cover the HTTP server (health/version/search/memory,
  type filtering, request-level RRF overrides, 404 handling).

### Fixed — `_backfill_embed_state` race on concurrent index opens

- **`Index._migrate()` no longer crashes with
  `IntegrityError: UNIQUE constraint failed: embed_state.memory_id`**
  when two connections (e.g. an MCP child and a CLI invocation) open
  the same vault simultaneously. Both SELECTs would return the same
  un-stamped rows; both would try to INSERT; the loser used to crash.
  Switched the helper's INSERT to `INSERT OR IGNORE` so the duplicate
  is silently skipped — the `NOT EXISTS` guard in the SELECT narrows
  the window but cannot close it.
- 2 new regression tests: a deterministic test that drives the
  helper's INSERT statement with a stale-view payload and verifies it
  doesn't raise, plus a source-level guard that asserts the SQL
  literally contains `INSERT OR IGNORE` so a future refactor cannot
  silently reintroduce the race.

## [0.5.0] — 2026-04-27

Four PRs shipped together that together close the loop on multi-agent
ingestion safety, OpenClaw transcript coverage, and configurable
ranking. The headline win: the daemon now ingests OpenClaw session
trajectories as full searchable transcripts (PR #36), so a search for
an exact phrase from yesterday's chat actually lands on the chat.
Combined with opt-in workspace discovery (PR #32), the per-workspace
layout schema (PR #33), and the search-config wiring fix (PR #35),
the v0.5.0 vault is a meaningfully better retrieval target than
v0.4.0 was — measured against a 12-query eval, top-5 went from 10/12
under the prior multi-agent install to 12/12 under the scoped + full-
transcript install.

### Changed — OpenClaw discovery is now opt-in

- **`memstem init` no longer auto-includes every OpenClaw workspace it
  finds.** On a multi-agent host (Ari + Blake + Charlie + …) the
  installer used to silently index all of them, mixing every agent's
  memory into one vault. The wizard now defaults each discovered agent
  to "no" — the user opts in explicitly, agent by agent. Shared files
  (`HARD-RULES.md`) follow the same opt-in model since they belong to a
  workspace.
- **`memstem init -y` (non-interactive) writes a Claude-Code-only config.**
  Previously `-y` meant "auto-include every discovered agent with
  content" — convenient but wrong on multi-agent installs. Now `-y`
  produces a conservative config; OpenClaw workspaces must be added by
  re-running `memstem init` interactively or by hand-editing
  `_meta/config.yaml`.
- **Existing installs are unaffected on disk** but should review their
  `agent_workspaces` list. To prune the index after removing entries
  from `config.yaml`, delete the corresponding directories under
  `<vault>/memories/openclaw/<tag>/` and `<vault>/daily/<tag>/` and
  re-run `memstem reindex`.

### Added — OpenClaw session trajectory ingestion

- **`*.trajectory.jsonl` files under a workspace's configured
  `session_dirs` are now ingested as `type:session` records.** Lets
  Memstem search the full transcript of every OpenClaw session, not
  just the distilled `[TECHNICAL]/[DECISION]/[RULE]` bullets that the
  upstream memory writer extracts. Search for an exact phrase from
  yesterday's chat now lands you on the exact session.
- New `OpenClawLayout.session_dirs` field — list of workspace-relative
  directories. Empty by default (opt-in). Set
  `["agents/main/sessions"]` for OpenClaw's standard layout.
- New `_parse_trajectory_file()` parses the OpenClaw event-log format,
  pulling `prompt.submitted.data.prompt` (user turns) and
  `model.completed.data.assistantTexts` (assistant turns) into a
  chronological transcript. Tool calls, context-compilation events,
  and trace artifacts are intentionally skipped — they're operational
  metadata that adds noise to a search index.
- Trajectory records carry `session_id`, `workspace_dir`, `agent_id`,
  `turn_count`, `created`, `updated` in metadata, with the agent tag
  applied by the workspace adapter (`agent:<tag>`).
- Watch loop also handles trajectory paths — incremental updates as
  the agent appends events get reflected in the index.
- 14 new tests covering parser correctness (turns, operational events,
  empty/malformed lines, metadata extraction), classification (in/out
  of session_dirs, suffix matching, default-empty), and end-to-end
  reconcile (default skip vs. opt-in include).

### Added — per-workspace layout overrides for OpenClaw

- **`OpenClawWorkspace` now accepts a `layout` field** specifying which
  paths inside the workspace get ingested. Lets toolkit users with
  non-canonical OpenClaw layouts (memories under `notes/` instead of
  `memory/`, skills disabled, custom `MEMORY.md` filename) point the
  adapter at their actual paths instead of forking the adapter or
  symlinking files.
- New `OpenClawLayout` model with five configurable fields, all with
  defaults that preserve current behavior:
  - `memory_md` — top-level core file (default `MEMORY.md`; `None` to skip).
  - `claude_md` — operational rules file (default `CLAUDE.md`; `None` to skip).
  - `memory_dirs` — list of directories whose `*.md` descendants are
    ingested (default `["memory"]`; empty list = no recursive ingestion).
  - `skills_dirs` — list of directories whose `**/SKILL.md` descendants
    are ingested (default `["skills"]`; empty list = no skills).
  - `session_dirs` — list of directories whose `*.trajectory.jsonl`
    descendants are ingested as session records (default `[]`; opt-in).
- Both reconcile (`_iter_workspace_files`) and watch
  (`_classify_workspace_path`) honor the layout, so live file events
  flow into the index using the same path conventions configured for
  reconcile.
- Existing configs are unchanged: omitting `layout` falls back to the
  canonical defaults via Pydantic's `default_factory`. 6 new tests cover
  the override paths (custom memory dir, skip MEMORY.md, skip skills,
  multiple memory dirs, default unchanged, watch classifier).

### Fixed — search config knobs are now actually read

- **`SearchConfig.rrf_k`, `bm25_weight`, and `vector_weight` were dead
  config.** The values lived in `_meta/config.yaml` but neither the CLI
  (`memstem search`) nor the MCP server read them — both call sites used
  the function defaults. So changing those values in config did nothing.
  Found while investigating ranking quality on a 12-query retrieval
  test: vault cleanup alone moved top-5 from 6/12 to 8/12, but tuning
  the (unused) weights showed no further improvement until the wiring
  was fixed.
- `Search.search()` now accepts `rrf_k`, `bm25_weight`, `vector_weight`
  parameters; the CLI threads `cfg.search.*` through, and the MCP
  server's `build_server()` accepts a `search_config: SearchConfig`
  kwarg that the daemon passes from the loaded config.
- `rrf_combine()` applies the weights as
  `score += weight / (k + rank)` per source. Default weights stay
  `1.0/1.0` so existing installs see no behavior change. Set
  `bm25_weight: 0` to make search vec-only, or vice versa. 5 new tests
  cover weight scaling, zero-weight short-circuit, and weighted
  overlap.

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
