# CLAUDE.md — Project context for Memstem

This file is loaded automatically by AI coding agents working in this repo.
Read it first; it tells you how to operate on this codebase.

## What this project is

**Memstem** is a unified memory + skill infrastructure for AI agents. It pulls memories from multiple AI client filesystems (Claude Code, OpenClaw, Codex, Cursor, etc.) via `inotify` watchers, stores them as markdown files with YAML frontmatter, indexes them with SQLite (FTS5 + sqlite-vec) hybrid search, and exposes a unified MCP API.

Architectural advantage: immune to upgrade churn in any client because we depend only on the files each AI drops on disk — no hooks, no push APIs, no internal SDKs.

## Where to start

1. **Read [README.md](./README.md)** for what's shipping and how it's used.
2. **Read [ARCHITECTURE.md](./ARCHITECTURE.md)** for the design.
3. **Read the ADRs in [docs/decisions/](./docs/decisions/)** for locked decisions. If a change touches storage, search ranking, or the adapter interface, write an ADR before implementing.
4. **Check [ROADMAP.md](./ROADMAP.md)** for what's planned vs. out of scope.

## How to work

- Branch from `main`: `git checkout -b feat/<area>` (or `fix/`, `docs/`, `chore/`)
- Tests for every new module
- `ruff format`, `ruff check`, `mypy src/` must pass before commit
- Pre-commit hooks enforce the above; don't bypass with `--no-verify`
- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`
- Open a PR for every change; CI must pass; squash on merge
- Update CHANGELOG.md (`[Unreleased]` section) for user-facing changes
- New dependencies need justification in the PR description

## What lives where

| Path | Purpose |
|---|---|
| `src/memstem/` | The package — `core/` (storage, index, search), `adapters/`, CLI, MCP + HTTP servers |
| `~/memstem-vault/` | Canonical markdown store (NOT inside the repo) |
| `~/memstem-vault/_meta/index.db` | SQLite index (rebuildable) |
| `~/memstem-vault/_meta/config.yaml` | Daemon config |

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

- If a decision touches storage, search ranking, or the adapter interface, write an ADR (`docs/decisions/NNNN-<slug>.md`) before implementing.
- If it's still not clear, open an issue or ask the maintainer before guessing.

## Quick reference

- System design: [ARCHITECTURE.md](./ARCHITECTURE.md)
- Roadmap (phases 1–5): [ROADMAP.md](./ROADMAP.md)
- Install guide: [docs/install.md](./docs/install.md)
- Frontmatter schema: [docs/frontmatter-spec.md](./docs/frontmatter-spec.md)
- MCP API: [docs/mcp-api.md](./docs/mcp-api.md)
- Decisions: [docs/decisions/](./docs/decisions/)
- Repo: https://github.com/Memstem/memstem
