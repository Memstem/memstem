# Memstem Implementation Plan

> The single source of truth for what's done, what's next, and how to work on Memstem.
> Read this first when starting a fresh session in `~/memstem/`.

## ▶ Resume here (last update: 2026-04-25 evening)

**Where things stand:** Phase 1 v0.1 codebase is feature-complete and merged on `main`. PRs #4–#17 all merged. 220 tests passing, 88% coverage. Live Ollama integration tests pass. Multi-agent OpenClaw + Claude Code adapter (sessions + extras) + setup wizard + installer + `memstem doctor` — all built. Three malformed Ari memory files patched in place. Dry-run shows ~940 records ready to ingest from Brad's box.

**Where Brad is:** ended session 2026-04-25 evening, plans to test the cutover tomorrow morning. He explicitly asked for independent work tonight that does NOT touch his live system or push tags.

### Independent to-dos for fresh-me to start (no Brad needed)

Pick them up in this order. They're all branch-from-main + PR + self-merge on green CI, the same pattern as PRs #4–#17.

1. [x] **PR #19 — `memstem connect-clients`** *(highest priority)*
   - New CLI command in `src/memstem/cli.py` that automates cutover wiring.
   - Edits `~/.claude.json` to add `mcpServers.memstem` (merge, don't overwrite other servers). Earlier drafts targeted `~/.claude/settings.json`, but current Claude Code releases ignore that block — see PR #30.
   - Adds/updates `<!-- memstem:directive v1 -->` blocks in `~/.claude/CLAUDE.md` and per-agent CLAUDE.md files.
   - `--dry-run` previews changes (diff style). Default mode writes `.bak` before edit. Idempotent.
   - Flags: `--claude-code` (default true), `--openclaw <path>` (repeatable), `--remove-flipclaw` (default false; comments out the FlipClaw `SessionEnd` hook).
   - Wire it into `install.sh --connect-clients` so the curl-pipe-bash flow ends with everything wired up.
   - Directive text below — agreed-on nuanced version, NOT "always use Memstem":

   ```markdown
   <!-- memstem:directive v1 -->
   ## Memory access (Memstem)

   For retrieval-style queries — "what did we decide about X?", "what's
   the plan for Y?", "do we have a skill for Z?" — search Memstem first
   via `memstem_search` (MCP) or `memstem search "query"` (CLI). It
   indexes every agent's memory + skills + Claude Code sessions with
   hybrid keyword + semantic search; a `grep` can't.

   For specific known files — `~/<agent>/MEMORY.md`, today's daily log,
   a specific SKILL.md you've been told to follow — read directly. That's
   faster and Memstem isn't trying to replace direct file access.

   Tools: memstem_search, memstem_get, memstem_list_skills,
   memstem_get_skill, memstem_upsert.
   <!-- /memstem:directive -->
   ```

   - Implementation suggestion: new `src/memstem/integration.py` module with `register_mcp_server(settings_path)`, `apply_directive(claude_md_path, directive_block)`, `remove_flipclaw_hook(settings_path)`. Tests in `tests/test_integration.py` using `tmp_path` (don't touch real `~/`).

2. [x] **ADR 0008 — Summarization & importance ranking design**
   - Pure design doc at `docs/decisions/0008-tiered-memory.md`. NO CODE.
   - Lay out the v0.2 tiered-memory plan that Brad asked about: importance scoring, distillations ("dreaming"), hygiene worker. See "Phase 2 plan — Tiered memory (v0.2)" section below for the agreed-on shape.
   - Brad will review the ADR before we start coding any of it.

3. [ ] **PR #21 — cross-platform CI** *(originally #20 in this list; ADR 0008 ate #20 because PR-before-merge applied to it too)*
   - Adds `macos-latest` and `windows-latest` to the test matrix.
   - Both are `continue-on-error: true`. macOS hits a known
     `actions/setup-python` issue (Python build without
     `enable_load_extension`); user-installed Python (Homebrew, python.org)
     works fine. Windows is WSL2-only for v0.1 by design.
   - README documents the support story.

4. [x] **PR #22 — README + v0.1 release prep** *(originally #21; renumbered)*
   - Honest "Install in one line, run the daemon, here's how to query" section in `README.md`.
   - Version bumped to `0.1.0` in `pyproject.toml` and `src/memstem/__init__.py`.
   - CHANGELOG entries rolled from `[Unreleased]` to `[0.1.0] - 2026-04-XX` (placeholder date — Brad fills in at tag time).
   - **Do NOT tag, do NOT push tags, do NOT publish to PyPI.** Those are Brad's calls after he validates cutover.

### Brad-required (do NOT start)

5. [ ] **Step 9 — cutover on the live box**: dry-run, audit a sample, `memstem migrate --apply`, `memstem connect-clients` (when PR #19 lands), `pm2 start memstem daemon`, soak. He explicitly gated this.
6. [ ] **Step 10 — actual `v0.1.0` tag + PyPI publish**. His call after the soak window.

### Working pattern reminder

- Branch from `main`: `git checkout -b feat/<area>` or `chore/<area>`
- Commit style: conventional prefix (`feat:`, `fix:`, `docs:`, `chore:`, `test:`).
- Every commit ends with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- PR before merge, even for solo work. Self-merge once CI is green (we've been doing this all session).
- Pre-commit hooks (ruff + ruff-format + mypy) must pass on every commit. They run `mypy src/ tests/` (PR #15 widened it from `src/` only).
- `.venv/bin/pytest` from the repo root. Coverage stays around 88%.

---

## Snapshot (2026-04-25, end-of-day)

- **Repo:** [memstem/memstem](https://github.com/memstem/memstem) (private; under the `Memstem` GitHub org)
- **Phase:** 1 (v0.1) feature-complete; awaiting cutover.
- **Lines of code:** ~3,500 src + ~3,000 tests
- **Tests passing:** 220 (5 deselected — Ollama integration tests, all pass when run with `-m requires_ollama`)
- **Coverage:** 88% overall; new modules typically 90%+
- **CI:** green on every merged PR
- **Decisions locked:** ADRs 0001–0007, 0009, 0010 are accepted in [`docs/decisions/`](./docs/decisions/). ADRs 0008 (tiered memory), 0011 (noise filter + fact extraction), 0012 (LLM-judge dedup) are proposed and awaiting Brad's review.
- **Live infra status:** Ollama 0.21.2 installed and running on `127.0.0.1:11434`, `nomic-embed-text` (768 dims) loaded.

### Merged PRs (full Phase 1 + extensions)

| PR | What |
|---|---|
| #4 | Phase 0 lock + skeleton mypy fix |
| #5 | `frontmatter` + `Vault` storage |
| #6 | embeddings (Ollama) + sqlite/vec index |
| #7 | hybrid search (RRF) |
| #8 | OpenClaw adapter |
| #9 | Claude Code adapter (sessions) |
| #10 | MCP server (5 tools) |
| #11 | Pipeline + CLI (init/daemon/search/reindex/mcp) |
| #12 | FlipClaw migration script |
| #13 | Multi-agent OpenClaw + role tags |
| #14 | `migrate.py` honors workspace config |
| #15 | `install.sh` + `memstem doctor` + ADR 0007 |
| #16 | Setup wizard in `memstem init` (`memstem.discovery`) |
| #17 | Claude Code `extra_files` (CLAUDE.md ingest) |

## Vision (one paragraph)

Memstem is a standalone memory + skill service that pulls from the filesystems of multiple AI clients (Claude Code, OpenClaw, Codex, Cursor, Hermes, Aider) via `inotify` watchers, stores everything as markdown files with YAML frontmatter, indexes them with SQLite (FTS5 + sqlite-vec) hybrid search, and exposes a unified MCP API. The architectural advantage: **immune to upgrade churn in any client** because we never depend on hooks, push APIs, or internal SDKs — only on the files each AI happens to drop on disk.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md). Phase plan: [ROADMAP.md](./ROADMAP.md).

## Phase 1 goal (v0.1)

**Running locally on the EC2 box, ingesting from Claude Code + Ari/OpenClaw in real-time, exposing MCP search to both, with FlipClaw retired.**

End state checklist:
- [x] All Phase 1 modules built (PRs #4–#17 merged) — code is feature-complete
- [ ] `memstem daemon` runs as a PM2 service alongside `ari`, `fleet-gateway`, etc.
- [ ] Real-time ingestion from `~/.claude/projects/` and `~/ari/memory/` (live; not just dry-run)
- [ ] MCP `memstem_search` returns relevant hybrid-ranked results within 250ms p95 (haven't measured yet)
- [x] Multi-agent OpenClaw ingestion (7 agents on Brad's box) and Claude Code session + extras adapter
- [ ] One-shot migration `--apply` actually run on the live box
- [ ] Both Claude Code and OpenClaw configs point at Memstem first (PR #19 automates this; not yet built)
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

- [x] `src/memstem/servers/mcp_server.py`
  - Built on `FastMCP` from the `mcp` Python SDK
  - `build_server(vault, index, embedder=None, name="memstem")` factory
  - Tools: `memstem_search`, `memstem_get`, `memstem_list_skills`, `memstem_get_skill`, `memstem_upsert`
  - Tool definitions match `docs/mcp-api.md`
  - stdio loop wired by the CLI's `memstem mcp` command (Step 7)
- [x] `tests/test_mcp_server.py` — in-process MCP client → server, real Vault + Index

### Step 7: CLI

- [x] `core/pipeline.py` — `Pipeline.process(record)` writes Memory to vault, upserts index, embeds chunks. Stable `(source, ref) → memory_id` mapping in a `record_map` table for idempotent re-emits.
- [x] Wire up `cli.py` stubs:
  - `memstem init <path>` — creates vault directory tree, writes `_meta/config.yaml`
  - `memstem daemon` — runs adapter reconcile + watch loop into the pipeline
  - `memstem search <query>` — one-shot CLI search
  - `memstem reindex` — rebuild the index by walking the canonical vault
  - `memstem mcp` — runs the MCP server on stdio (used by Claude Code)
- [x] `tests/test_cli.py` — typer's CliRunner; `tests/test_pipeline.py` for ingestion logic

### Step 8: Migration from FlipClaw

- [x] `scripts/migrate-from-flipclaw.py` (thin wrapper) + `memstem.migrate` (testable module)
  - Import `~/ari/memory/*.md` → `~/memstem-vault/memories/openclaw/`
  - Import `~/ari/skills/*/SKILL.md` → `~/memstem-vault/skills/<slug>.md`
  - Import recent (last 30 days) Claude Code sessions from `~/.claude/projects/*/`
  - Preserve creation dates via the adapter's mtime fallback
  - Tag every record with `flipclaw-migration` (idempotent)
  - Dry-run mode (default) and `--apply` flag
- [ ] Run dry-run on the live box, audit a sample of 20 records by hand (Step 9)
- [ ] Run `--apply`, verify counts (Step 9)

### Step 8.5: Multi-agent + extra-files ingestion (added after dry-run review)

The original adapter only saw `~/ari/memory/` and `~/ari/skills/`. After
inspecting the live box (7 OpenClaw agent workspaces, plus shared rules
and Claude Code instructions) we extended scope before cutover.

- [x] `memstem.config.OpenClawWorkspace`, `OpenClawAdapterConfig`,
  `ClaudeCodeAdapterConfig`, `AdaptersConfig` — typed config blocks for
  per-agent workspaces and shared/extra files
- [x] `OpenClawAdapter` workspace mode: walks `<workspace>/MEMORY.md`,
  `<workspace>/CLAUDE.md`, `<workspace>/memory/*.md`, and
  `<workspace>/skills/*/SKILL.md` per workspace, tagging records with
  `agent:<tag>` (and `core` / `instructions` for top-level files).
  Shared files emit with a `shared` tag. Legacy paths-only mode
  preserved for back-compat
- [x] CLI daemon reads adapter config and switches mode automatically
- [x] 16 new tests for workspace mode + watch
- [x] `memstem.migrate` honors workspace config (so the migration emits
  records with the right `agent:<tag>` per source)
- [x] **PR #15:** `scripts/install.sh` (one-line installer with `--yes` /
  `--no-ollama` / `--vault` flags) and `memstem doctor` CLI command —
  unblocks the agent-driven install path (`curl ... | bash -s -- --yes`).
  ADR 0007 documents the remote-ingestion design choice (sync-and-watch
  for v0.1; HTTP push as Phase 3 nice-to-have; full multi-device sync in
  Phase 4)
- [x] **PR #16:** setup wizard in `memstem init` — `memstem.discovery`
  finds OpenClaw candidates, HARD-RULES.md shared files, and Claude Code
  paths. `init` defaults to interactive per-candidate prompts; `-y` /
  `--non-interactive` auto-includes every candidate with content. `--home`
  lets tests and headless runs scope the discovery
- [x] **PR #17:** ClaudeCodeAdapter `extra_files` — `~/.claude/CLAUDE.md`
  (and any project-level or otherwise configured CLAUDE.md) ingest as
  `instructions`-tagged records (type=memory). Watch picks up changes
  via the parent dir; reconcile yields them alongside session JSONLs.
  CLI daemon reads `cfg.adapters.claude_code.extra_files` and passes
  them through

### Step 8.6: Phase 1.5 — pre-cutover automation (added 2026-04-25 evening)

These came out of the discussion about "agents installing this for the user" and the multi-config question. Strictly nice-to-have for v0.1, but each of them removes manual labor from Step 9 cutover so they're worth landing first.

- [x] **PR #19 — `memstem connect-clients`** (the headline; details under "Resume here" above)
- [x] **ADR 0008 — tiered memory design** (no code; sets the v0.2 direction)
- [ ] **PR #21 — cross-platform CI** (macOS + Windows jobs, both `continue-on-error: true`)
- [x] **PR #22 — README + version bump to `0.1.0`** (no tag, no PyPI publish)

### Step 9: Integration and cutover (live box, requires Brad)

The nuanced directive (rather than the original "always search Memstem first") was agreed in the 2026-04-25 evening session — see "Resume here" for the v1 template.

- [ ] **`memstem migrate --apply`** — runs through ~940 records, populates `~/memstem-vault/` and the index. Pre-flight: re-run dry-run first, audit ~20 sample records by hand. Time estimate: 2–5 minutes (mostly embedding).
- [ ] **`pm2 start memstem daemon --name memstem`** then `pm2 save`. Verify with `pm2 logs memstem` that reconcile completed and the watch loop is running.
- [x] **`memstem connect-clients`** — adds the MCP server registration to `~/.claude.json` (PR #30 moved it from `~/.claude/settings.json`, which Claude Code no longer reads for MCP) and the directive blocks to every CLAUDE.md. Backups everywhere; `--dry-run` first. Auto-cleans the stale `mcpServers.memstem` entry from the legacy `~/.claude/settings.json` on each run.
- [ ] **Soak for 24h** — query via `memstem search` periodically; verify retrieval quality. Fix any issues that surface.
- [ ] **Disable FlipClaw bridge** — comment out the `SessionEnd` hook in `~/.claude/settings.json` (hooks still live there; only MCP server config moved). `memstem connect-clients --remove-flipclaw` does it.
- [ ] **Disable Ari's `incremental-memory-capture.py` cron entry**.
- [ ] **7-day soak with FlipClaw disabled** — verify Memstem alone keeps memory fresh.

### Step 10: v0.1 release (after soak, requires Brad)

- [ ] Confirm CHANGELOG entries are correct and complete (PR #21 stages them)
- [ ] `git tag v0.1.0 && git push --tags`
- [ ] Reserve `memstem` on PyPI; publish with `pipx run hatch publish` (or equivalent)
- [ ] Update `install.sh` to default to PyPI source (currently falls back to git)
- [ ] Flip the GitHub repo public if Brad is happy with the result
- [ ] Open Phase 2 entry: start coding ADR 0008's tiered-memory plan

## Phase 2 plan — Tiered memory (v0.2)

This is the design that came out of Brad's "should we add summarization / dreaming?" question on 2026-04-25. ADR 0008 will formalize it; this section is the working summary.

The core insight: raw retrieval over ~940 records works, but doesn't scale or self-improve. A query for "what did we decide about pricing" gets noisy when there are 17 partial discussions across daily logs and session transcripts. The fix is to layer **scoring**, **distillation**, and **dedup** on top of the raw store.

### Tier 0 — Raw memories (already in v0.1)

Every adapted file as a single record. Source of truth, never deleted by the system. ~940 records on Brad's box at cutover.

### Tier 1 — Importance scoring

Each memory carries an `importance: 0.0–1.0` field on its frontmatter. The schema already supports this; v0.1 doesn't populate it. v0.2 will.

**Heuristic seed (computed at ingest):**
- Type weight: `skill` (0.7) > `decision` (0.6) > `memory` (0.5) > `session` (0.3). Skills are intentional learnings; sessions are conversational and most of their content is incidental.
- Recency: linear decay from 1.0 at creation to 0.5 at 90 days; constant after.
- Wikilink density: each `[[X]]` reference inbound from another memory adds 0.05, capped at +0.3.
- Length penalty: very short memories (< 100 chars) drop 0.1 (probably not useful on their own).

**Live boost from query traffic:**
- The MCP server logs every `memstem_search` result and every `memstem_get` open. (New: a `query_log` table in `_meta/index.db`.)
- The hygiene worker periodically reads this log and bumps `importance` on memories that appear in successful retrievals (heuristic: weighted by how high they ranked + whether the user opened them).

**Manual pin:** `memstem pin <id>` locks `importance=1.0` and disables decay.

**Search ranking:** `final_score = rrf_score * (1 + α * importance)`. α = 0.2 so importance is a tiebreaker, not a forcing function. Pins effectively double the score.

### Tier 2 — Distillations (the "dreaming" pass)

Background `memstem.hygiene` worker (already a stub package) periodically clusters related raw memories and asks an LLM to write a summary. Output is a new memory with `type: distillation` (new schema enum value).

**Distillation memory shape:**
```yaml
---
id: <uuid>
type: distillation
title: "Cloudflare decisions and migration plan"
created: 2026-04-26T03:00:00Z
updated: 2026-04-26T03:00:00Z
source: hygiene-worker
provenance:
  source: hygiene-worker
  ref: "topic-cluster:cloudflare"
  ingested_at: 2026-04-26T03:00:00Z
links:
  - "[[memory://memories/openclaw/abc-123]]"
  - "[[memory://memories/openclaw/def-456]]"
  - "[[memory://sessions/xyz-789]]"
importance: 0.8
---

Across 12 conversations and daily logs over March-April 2026 we settled
on Cloudflare for new domains because at-cost pricing saves ~$1,200/yr
across 100 domains. GoDaddy renewal pricing is 2x+. Migration plan...
```

**Two flavors to ship first:**

1. **Session distillation.** Long Claude Code sessions (30+ turns or > 10k tokens) get distilled into a 1-paragraph "decisions / learnings" note. Source session links back. The agent searching for "what did we decide on Tuesday" sees the distillation; if it needs the verbatim transcript, one click via `links`.

2. **Topic distillation.** Cluster raw memories by embedding similarity. For each cluster of 5+ related memories, write a "What we know about X" rollup. Run weekly; old distillations are superseded by newer ones (`deprecated_by:` field).

**Search ranking with distillations:**
- Default search returns a mix; distillations get `+α * 0.3` to importance because they save context.
- Agent can request `types=[distillation]` for high-level rollups only, or `types=[memory, session]` for primary sources only.

**LLM choice:** start with Ollama running a bigger model (e.g. `llama3.2:8b` or `qwen2.5:7b`). Local-first stays consistent with the embedding choice. Optional config to route to a Claude/OpenAI call for higher-quality distillations. Cost: at the volume we'd run (a few clusters per day), API option is cents/month.

### Tier 3 — Hygiene worker (dedup + decay + housekeeping)

Already on ROADMAP as Phase 2 work. Specifically:

- **Dedup**: ADR 0012 supersedes the original simple-cosine sketch with a three-layer pipeline (exact body-hash → embedding candidates → LLM-as-judge using Graphiti's MIT-licensed prompts). Resolution invalidates rather than deletes (`deprecated_by` / `valid_to` / `supersedes`). Skills route to a human-review queue (`memstem skill-review`); never auto-merged.
- **Decay**: importance falls over time per the curve in Tier 1. Decayed memories that haven't been retrieved in 6 months drop below the search threshold by default (still findable with `--include-decayed`).
- **5-minute reconciliation pass** to catch files that changed while the daemon was offline. (Not in v0.1; documented as a v0.2 follow-up.)
- **Bi-temporal validity**: when a fact contradicts an existing one (LLM-judged or explicit `supersedes:` field), the old gets `valid_to:` rather than being deleted. Search defaults to "currently valid" but historical queries are possible.

> Skill authoring is explicitly **out of scope**. Each AI generates
> skills its own way; MemStem only ingests `SKILL.md` files from
> disk. See [ADR 0019](docs/decisions/0019-no-skill-authoring.md).

### ADRs 0011 + 0012 — Quality pipeline (proposed 2026-04-27)

Two ADRs narrow the v0.2 plan to address the mem0-audit failure modes (97.8% of 10,134 captured entries were junk after 32 days; 808 duplicate copies traced to one hallucinated fact). Both are 100% MemStem-internal — nothing in Claude Code or OpenClaw changes — and run on Linux + macOS today (Python + local Ollama, no platform-specific deps; Windows targeted for later).

- **ADR 0011 — Write-time noise filter + atomic-fact extraction** ([decisions/0011](./docs/decisions/0011-noise-filter-and-fact-extraction.md)). Inserts a `core/extraction.py` stage between adapter output and pipeline upsert. Phase A: deterministic heuristic filter drops heartbeats / cron output / boot-file echoes; tags transient task state with a 4-week `valid_to`. Phase B: local Ollama (`qwen2.5:7b`) splits long sessions into atomic `type: fact` records, preserving the original as `type: session_raw` (excluded from default search, still linkable for audit). Adds two `type` enum values and one frontmatter field. Phasing: PR-A heuristics → PR-B TTL tagging → PR-C boot-echo hash → PR-D LLM extraction → PR-E `session_raw` filter.
- **ADR 0012 — Two-stage dedup with LLM-as-judge** ([decisions/0012](./docs/decisions/0012-llm-judge-dedup.md)). Replaces the simple cosine-≥0.95 step from Tier 3 above. Three layers: (1) exact body-hash dedup at write time (catches the hallucination feedback loop for free), (2) embedding-cosine candidate generation, (3) LLM judge using Graphiti's MIT-licensed prompts with explicit failure-mode rules. Resolution uses existing schema fields (`deprecated_by` / `valid_to` / `supersedes`); no record is ever deleted. Skills route to `vault/skills/_review/` and require manual `memstem skill-review apply`. Phasing: PR-A hash layer → PR-B candidate query → PR-C judge + audit log → PR-D resolution actions → PR-E skill-review CLI.

Both depend on the same local Ollama model that Tier 2 distillations will use, so install footprint stays the same. Each ADR's PR-A (heuristic filter, hash-layer dedup) is the cheapest, most isolated first piece — good parallel starting points once the ADRs land.

### Other Phase 2 items already on ROADMAP

- **Anthropic memory tool adapter** (ADR 0006): implement `BetaAbstractMemoryTool` so Claude Code's official memory tool routes natively into Memstem.
- **HTTP API** alongside MCP for non-MCP clients.
- **Backup**: nightly `git push` of the vault to a private remote.
- **Documentation site** at `memstem.com` (we own the domain).

## Cross-platform support

Current state:
- **Linux**: fully tested and shipped. CI runs Python 3.11 + 3.12.
- **macOS**: should work — `watchdog` uses FSEvents on Darwin, `install.sh` already accepts `Darwin*` in its uname check. Untested. PR #20 adds CI to confirm.
- **Windows**: not supported in v0.1. WSL2 is the documented workaround. v0.2+ would need a parallel `install.ps1` PowerShell installer plus path-handling audit. PR #20 adds Windows to CI as `continue-on-error: true` so we get visibility on what breaks.

## Architectural decisions made in this session (2026-04-25)

For continuity. Most are already locked into ADRs or PRs; this is the consolidated view.

- **Skill ingestion: strict.** Only `<workspace>/skills/<slug>/SKILL.md` — not every `.md` under each skill folder. v0.2 can loosen if needed; for now strict avoids `INSTALL.md` / `README.md` polluting search results.
- **Three malformed Ari memory files patched in place.** Leading `---` removed from `2026-02-24.md`, `2026-02-28.md`, `2026-03-10.md`. They were being misread as YAML frontmatter; removing the rule fixed parsing. v0.2 should improve adapter robustness so this isn't manual: when `frontmatter.loads` fails, fall back to "no frontmatter, raw body."
- **Multi-agent OpenClaw via `OpenClawWorkspace(path, tag)` config.** Per-agent records carry `agent:<tag>` plus role tags (`core` for MEMORY.md, `instructions` for CLAUDE.md). Shared files use `shared` tag. Setup wizard auto-discovers candidates. (PRs #13, #16.)
- **Claude Code extras**: `~/.claude/CLAUDE.md` and per-project CLAUDE.md ingest as `instructions`-tagged memory records. (PR #17.)
- **Remote ingestion deferred to Phase 3+**. ADR 0007: sync-and-watch (rsync, syncthing, iCloud) is the recommended workaround for v0.1. HTTP push is Phase 3 (v0.3); full multi-device sync stays Phase 4 (v0.4).
- **MCP vs. direct file access**: not redundant. MCP (and the `memstem search` CLI) is for ranked semantic search across the index; direct file reads stay valid for known files. The CLAUDE.md directive (PR #19) reflects this nuance — "Memstem for retrieval queries; read directly when you know the file."
- **"Agent installs this" framing**: realized via `install.sh --yes --connect-clients` (PR #19 wires the second flag). Agent runs one curl-pipe-bash; user sees nothing.
- **Credentials are not memories.** Brad agreed with the framing: shared `~/.openclaw/shared-auth-profiles.json` etc. stay in their dedicated stores. Memstem indexes the **knowledge about where credentials live** (the natural-language "OpenClaw agents use OAuth from X" facts in MEMORY.md / HARD-RULES.md), not the secrets themselves.
- **Migration scope**: ~940 records ingestable on day one (515 ari + 53 sarah + small amounts from other agents + 1 shared HARD-RULES.md + 356 Claude Code sessions). Confirmed via dry-run.

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
