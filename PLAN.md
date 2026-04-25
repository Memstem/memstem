# Memstem Implementation Plan

> The single source of truth for what's done, what's next, and how to work on Memstem.
> Read this first when starting a fresh session in `~/memstem/`.

## Snapshot (2026-04-25)

- **Repo:** [memstem/memstem](https://github.com/memstem/memstem) (private; lives under the `Memstem` GitHub org)
- **Phase:** 0 → entering Phase 1 implementation
- **Lines of code:** ~150 (skeleton only)
- **Tests passing:** 0 (none written yet)
- **CI:** configured, will fail until first test exists
- **Decisions locked:** ADRs 0001-0006 in [`docs/decisions/`](./docs/decisions/)

## Vision (one paragraph)

Memstem is a standalone memory + skill service that pulls from the filesystems of multiple AI clients (Claude Code, OpenClaw, Codex, Cursor, Hermes, Aider) via `inotify` watchers, stores everything as markdown files with YAML frontmatter, indexes them with SQLite (FTS5 + sqlite-vec) hybrid search, and exposes a unified MCP API. The architectural advantage: **immune to upgrade churn in any client** because we never depend on hooks, push APIs, or internal SDKs — only on the files each AI happens to drop on disk.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md). Phase plan: [ROADMAP.md](./ROADMAP.md).

## Phase 1 goal (v0.1)

**Running locally on the EC2 box, ingesting from Claude Code + Ari/OpenClaw in real-time, exposing MCP search to both, with FlipClaw retired.**

End state checklist:
- [ ] `memstem daemon` runs as a PM2 service alongside `ari`, `fleet-gateway`, etc.
- [ ] Real-time ingestion from `~/.claude/projects/` and `~/ari/memory/`
- [ ] MCP `memstem_search` returns relevant hybrid-ranked results within 250ms p95
- [ ] One-shot migration imports historical FlipClaw memory cleanly
- [ ] Both Claude Code and OpenClaw configs point at Memstem first (with local fallback)
- [ ] FlipClaw bridge + Ari capture sweep are disabled and unused for 7 days

## Phase 1 detailed to-do list

Order is roughly dependency-respecting; you can work top-down without backtracking.

### Step 0: Dev environment (do once)

- [ ] `cd ~/memstem && python3.11 -m venv .venv`
- [ ] `source .venv/bin/activate && pip install -e ".[dev]"`
- [ ] `pre-commit install` — installs git hooks
- [ ] `pytest` — should run zero tests but exit 0
- [ ] `ruff check .` — should pass
- [ ] `mypy src/` — should pass on the skeleton
- [ ] Verify Ollama is reachable: `curl http://localhost:11434/api/tags`
- [ ] Pull embedding model if missing: `ollama pull nomic-embed-text`

### Step 1: Frontmatter + storage layer

- [x] `src/memstem/core/frontmatter.py` — parse/serialize YAML frontmatter (use `python-frontmatter`)
  - `parse(content: str) -> tuple[dict, str]`
  - `serialize(metadata: dict, body: str) -> str`
  - Validation against the schema in `docs/frontmatter-spec.md`
- [x] `src/memstem/core/storage.py` — vault read/write/walk
  - `Vault` class wrapping a vault root path
  - `Vault.read(path) -> Memory`
  - `Vault.write(memory) -> None`
  - `Vault.walk(types: list[str] | None = None) -> Iterator[Memory]`
  - `Vault.delete(path) -> None`
  - `Memory` Pydantic model (frontmatter + body + path)
- [x] `tests/test_frontmatter.py` — round-trip tests, edge cases
- [x] `tests/test_storage.py` — vault CRUD + walk

### Step 2: Index layer

- [x] `src/memstem/core/index.py` — SQLite + FTS5 + sqlite-vec setup
  - Tables: `memories`, `memories_fts` (FTS5 virtual), `memories_vec` (sqlite-vec virtual), `tags`, `links`
  - `Index` class with `connect`, `upsert`, `upsert_vectors`, `delete`, `query_fts`, `query_vec`
  - Migration system (single `schema_version` row)
  - Indexed columns: id, type, source, created, updated, importance
- [x] `src/memstem/core/embeddings.py` — Ollama HTTP client
  - `OllamaEmbedder(base_url, model)`
  - `embed(text: str) -> list[float]`
  - `embed_batch(texts: list[str]) -> list[list[float]]`
  - Chunk strategy for long text (split at paragraphs, max 2048 chars)
- [x] `tests/test_index.py` — schema creation + basic CRUD
- [x] `tests/test_embeddings.py` — smoke test against running Ollama (mark with `@pytest.mark.requires_ollama`)

### Step 3: Hybrid search

- [x] `src/memstem/core/search.py`
  - `Search(vault, index, embedder)`
  - `query_bm25(query: str, limit: int) -> list[Hit]`
  - `query_vec(query_embedding: list[float], limit: int) -> list[Hit]`
  - `rrf_combine(bm25_hits, vec_hits, k=60) -> list[Hit]`
  - `search(query: str, limit: int = 10, types: list | None = None) -> list[Result]`
- [x] `tests/test_search.py` — fixture corpus of 20 sample memories, verify hybrid recall

### Step 4: OpenClaw adapter (do this first — easier)

- [x] `src/memstem/adapters/openclaw.py`
  - Subclass `Adapter`
  - Watch paths: `~/ari/memory/*.md`, `~/ari/skills/*/SKILL.md`, daily logs
  - `reconcile()`: walk paths, yield `MemoryRecord` for each file
  - `watch()`: use `watchdog.observers.Observer` for inotify, yield records on file events
  - File-to-record conversion: existing files are already markdown; just normalize frontmatter
- [x] `tests/adapters/test_openclaw.py` — fixture vault, verify reconcile picks up files

### Step 5: Claude Code adapter

- [x] `src/memstem/adapters/claude_code.py`
  - Watch paths: `~/.claude/projects/*/<session-uuid>.jsonl`
  - `reconcile()`: parse complete JSONL files, extract `(role, content)` turns
  - `watch()`: re-emit on file change (idempotent — pipeline upserts by `ref`)
  - One `MemoryRecord` per session with type=`session`, body=concatenated turns
  - Tool blocks summarized (`[tool_use: Bash]`, `[tool_result]`) so the body stays readable
  - Title: `ai-title` if present, else first user prompt truncated, else `session <uuid8>`
  - Future: per-line offset tracking for incremental ingestion (v0.2)
- [x] `tests/adapters/test_claude_code.py` — fixture JSONL, verify ingestion

### Step 6: MCP server

- [ ] `src/memstem/servers/mcp_server.py`
  - Use `mcp` Python SDK (already in dependencies)
  - Tools: `memstem_search`, `memstem_get`, `memstem_list_skills`, `memstem_get_skill`, `memstem_upsert`
  - Tool definitions match `docs/mcp-api.md`
  - stdio loop
- [ ] `tests/test_mcp_server.py` — in-process MCP client → server → mock vault

### Step 7: CLI

- [ ] Wire up `cli.py` stubs:
  - `memstem init <path>` — creates vault directory tree, writes `_meta/config.yaml`
  - `memstem daemon` — starts adapters + MCP server + (later) hygiene worker
  - `memstem search <query>` — one-shot CLI search
  - `memstem reindex` — wipe + rebuild index from canonical
  - `memstem mcp` — alias for daemon in MCP-only mode (used by Claude Code)
- [ ] `tests/test_cli.py` — typer's CliRunner

### Step 8: Migration from FlipClaw

- [ ] `scripts/migrate-from-flipclaw.py`
  - Import `~/ari/memory/*.md` → `~/memstem-vault/memories/`
  - Import `~/ari/skills/*/SKILL.md` → `~/memstem-vault/skills/`
  - Import recent (last 30 days) Claude Code sessions from `~/.claude/projects/*/`
  - Preserve creation dates from filesystem mtimes where possible
  - Tag everything with `provenance: flipclaw-migration`
  - Dry-run mode (default) and `--apply` flag
- [ ] Run dry-run, audit a sample of 20 records by hand
- [ ] Run `--apply`, verify counts

### Step 9: Integration and cutover

- [ ] Register Memstem MCP server in `~/.claude/settings.json`
  ```json
  "mcpServers": {
    "memstem": { "command": "memstem", "args": ["mcp"] }
  }
  ```
- [ ] Update `~/.claude/CLAUDE.md` to say "Always search Memstem first via `memstem_search` before answering questions about prior work, configuration, or skills"
- [ ] Update each agent's CLAUDE.md (`~/ari/CLAUDE.md`, `~/blake/CLAUDE.md`, etc.) similarly
- [ ] Add Memstem to OpenClaw config (per-agent)
- [ ] Set up `pm2 start memstem daemon --name memstem`, save with `pm2 save`
- [ ] Disable FlipClaw bridge: comment out the `SessionEnd` hook in `~/.claude/settings.json`
- [ ] Disable Ari's `incremental-memory-capture.py` cron entry
- [ ] Run for 7 days; verify retrieval quality, fix issues as they surface

### Step 10: v0.1 release

- [ ] `version` bump to `0.1.0` in `pyproject.toml` and `__init__.py`
- [ ] Update `CHANGELOG.md` with v0.1 highlights
- [ ] Tag release: `git tag v0.1.0 && git push --tags`
- [ ] Write a "what's working" section in README
- [ ] Decide on Phase 2 entry (start ADR 0007)

## Working conventions

### Git workflow
- Branch from `main`: `git checkout -b feat/storage-layer`
- Small commits, conventional-commit prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`)
- PR before merge, even for solo work — CI must pass
- Squash on merge for cleaner history (or rebase, your call)

### Code style
- ruff format + ruff check (strict — settings in `pyproject.toml`)
- mypy strict
- Public APIs have docstrings; types via `from __future__ import annotations`
- Tests for every new module
- No new dependencies without a brief note in PR description

### What lives where
- Vault root: `~/memstem-vault/` (NOT inside the repo)
- Index: `~/memstem-vault/_meta/index.db`
- Repo: `~/memstem/`
- Logs: `~/memstem-vault/_meta/logs/` (and `~/.pm2/logs/memstem-*` for PM2)

## Anti-patterns (don't do these)

- ❌ Don't write to the index without going through `core/storage.py` — keep the canonical-first invariant
- ❌ Don't put adapter logic in `core/` — adapters live in `adapters/` only
- ❌ Don't push to `main` directly
- ❌ Don't bypass pre-commit hooks (`--no-verify`)
- ❌ Don't add `requests` (use `httpx`) or `pyyaml` (use `python-frontmatter`)
- ❌ Don't swallow exceptions in adapter watchers — log them with full context
- ❌ Don't store secrets in the vault — `.env` and credentials stay outside

## Open questions / parking lot

- [x] Create `memstem` GitHub org and transfer repo (done 2026-04-25 — repo now at `memstem/memstem`)
- [ ] Buy `memstem.com` and `memstem.ai` (recommend: this week)
- [ ] Reserve `memstem` on PyPI + npm with stub packages (recommend: before v0.1 release)
- [ ] When to flip the repo public (recommend: end of Phase 2, when it's actually working)
- [ ] Whether to implement Anthropic memory tool adapter in Phase 1 or Phase 2 (current plan: Phase 2)
- [ ] Hygiene worker timing: continuous or scheduled? (decide in Phase 2)
- [ ] Multi-device sync strategy (Phase 4)

## How to use this document

If you're a fresh Claude Code session in `~/memstem/`:

1. Read this file completely
2. Read [ARCHITECTURE.md](./ARCHITECTURE.md) and the relevant ADRs in [`docs/decisions/`](./docs/decisions/)
3. Pick the first unchecked top-level item from the Phase 1 to-do list
4. If it's not 100% clear what to do, **ask Brad** before guessing
5. Work in a feature branch; open a PR; CI must pass
6. Check off the item in this file when merged
7. Update CHANGELOG.md as you go

If you're Brad:
- Update this file when scope changes
- Use it as the agenda for the daily heartbeat
- Don't let it drift — out-of-date plan is worse than no plan
