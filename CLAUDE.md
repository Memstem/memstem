# CLAUDE.md — Project context for Memstem

This file is loaded automatically by Claude Code when working in `~/memstem/`.
Read it first; it tells you how to operate on this codebase.

## What this project is

**Memstem** is a unified memory + skill infrastructure for AI agents. It pulls memories from multiple AI client filesystems (Claude Code, OpenClaw, Codex, Cursor, etc.) via `inotify` watchers, stores them as markdown files with YAML frontmatter, indexes them with SQLite (FTS5 + sqlite-vec) hybrid search, and exposes a unified MCP API.

Architectural advantage: immune to upgrade churn in any client because we depend only on the files each AI drops on disk — no hooks, no push APIs, no internal SDKs.

This will replace the current FlipClaw / Ari memory pipeline.

## Where to start every session

1. **Read [`PLAN.md`](./PLAN.md) first.** It has the current state, the full Phase 1 to-do list, and conventions.
2. **Read [`ARCHITECTURE.md`](./ARCHITECTURE.md)** for the design.
3. **Read the ADRs in [`docs/decisions/`](./docs/decisions/)** for locked decisions.

## Current phase

**Phase 1: v0.1 implementation.** Goal is a working local daemon that ingests from Claude Code + Ari, exposes MCP search, and lets us retire FlipClaw.

The repo is private at https://github.com/memstem/memstem. Source skeleton is in place; nothing is implemented yet.

## How to work

- **Read PLAN.md, pick the first unchecked Phase 1 to-do, work top-down.**
- Branch from `main`: `git checkout -b feat/<area>`
- Tests for every new module
- `ruff format`, `ruff check`, `mypy src/` must pass before commit
- Pre-commit hooks enforce the above; don't bypass with `--no-verify`
- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`
- Open a PR for every change; CI must pass; squash on merge
- Update PLAN.md checkboxes as items complete
- Update CHANGELOG.md for user-facing changes

## What lives where

| Path | Purpose |
|---|---|
| `~/memstem/` | This repo |
| `~/memstem-vault/` | Canonical markdown store (NOT inside the repo) |
| `~/memstem-vault/_meta/index.db` | SQLite index (rebuildable) |
| `~/memstem-vault/_meta/config.yaml` | Daemon config |

## Memory system rule (still applies)

The user's global `~/.claude/CLAUDE.md` says: do NOT use Claude Code's built-in memory directory; Ari is the source of truth for personal/organizational knowledge.

**This still applies during Memstem development.** The Memstem codebase is engineering work; it doesn't override the personal-memory rule. Continue to:

- Read Ari's memory via `cd ~/ari && OPENCLAW_CONFIG_PATH=/home/ubuntu/ari/openclaw.json openclaw memory search "query"`
- Check `~/ari/MEMORY.md` and `~/ari/memory/*.md` for facts
- Don't write to `~/.claude/projects/-home-ubuntu-memstem/memory/` or anywhere else under Claude Code's memory dir

Once Memstem itself is working in Phase 1, this changes — Memstem's vault becomes the source of truth and the `MEMSTEM_VAULT` env var points sessions there. But until then, follow the existing rule.

## Conventions specific to this project

### Storage invariant
**Markdown files are canonical. The SQLite index is derived and rebuildable.** Never write to the index without going through `core/storage.py` first. If you ever need to debug, trust the markdown files over the index.

### Adapter discipline
Adapters live in `src/memstem/adapters/`. They produce normalized `MemoryRecord` objects and never touch the index directly. Per-AI logic goes here, not in `core/`.

### Async + threads
- File watching: `watchdog` runs in a thread; events are pushed to an `asyncio.Queue`
- HTTP/MCP servers: async
- Index writes: synchronous (sqlite is fine for our scale)

### Test layout
- `tests/test_<module>.py` for `src/memstem/core/<module>.py`
- `tests/adapters/test_<adapter>.py` for adapters
- Mark Ollama/network tests with `@pytest.mark.requires_ollama` and skip in CI by default

## When in doubt

- If you don't know what to do, **ask Brad** before guessing.
- If a decision touches storage, search ranking, or the adapter interface, write an ADR (`docs/decisions/NNNN-<slug>.md`) before implementing.
- If you need to add a new dependency, justify it in the PR description.

## Quick reference

- Full plan + to-do list: [`PLAN.md`](./PLAN.md)
- System design: [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Roadmap (phases 1-5): [`ROADMAP.md`](./ROADMAP.md)
- Frontmatter schema: [`docs/frontmatter-spec.md`](./docs/frontmatter-spec.md)
- MCP API: [`docs/mcp-api.md`](./docs/mcp-api.md)
- Decisions: [`docs/decisions/`](./docs/decisions/)
- Repo: https://github.com/memstem/memstem
