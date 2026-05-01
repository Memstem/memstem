# Roadmap

## Phase 0 — Foundations (current)

- [x] Repo scaffold, docs, CI
- [x] Frontmatter spec finalized
- [x] MCP tool definitions finalized
- [x] Adapter interface defined

## Phase 1 — v0.1 / v0.2 (single-user, local, working) — shipped

- [x] Markdown canonical storage layer (read/write/walk)
- [x] SQLite index with FTS5 + sqlite-vec
- [x] Pluggable embedder backends — Ollama (default), OpenAI, Gemini (`gemini-embedding-2-preview` default + Matryoshka), Voyage
- [x] Always-on embed queue with retry/backoff (`memstem embed` for manual drains)
- [x] Hybrid search with RRF
- [x] Claude Code adapter (session JSONL watcher) + extras-ingest for `~/.claude/CLAUDE.md`
- [x] OpenClaw adapter (memory dir watcher) — multi-agent
- [x] MCP server with `memstem_search`, `memstem_get`, `memstem_list_skills`, `memstem_get_skill`, `memstem_upsert`
- [x] CLI: `memstem init`, `daemon`, `search`, `reindex`, `embed`, `migrate`, `mcp`, `doctor`, `connect-clients`
- [x] Migration script: import existing `~/ari/memory/` + Claude Code sessions
- [x] `memstem connect-clients` registers the MCP server in `~/.claude.json` and patches CLAUDE.md
- [x] `install.sh` end-to-end installer + `memstem doctor`
- [x] Cross-platform CI matrix (Linux + macOS + Windows)

**Goal:** running locally, replacing FlipClaw end-to-end. Achieved in v0.2.0.

## Phase 2 — v0.2–v0.9 (production-ready single-user)

- [x] Hygiene worker scaffolding + retro cleanup ([ADR 0008](./docs/decisions/0008-tiered-memory.md))
- [x] Importance scoring at ingest + live boost from query traffic
- [x] Cross-encoder rerank (W5, [ADR 0017](./docs/decisions/0017-cross-encoder-rerank.md))
- [x] HyDE query expansion (W6, [ADR 0018](./docs/decisions/0018-hyde-query-expansion.md))
- [x] LLM-as-judge dedup audit log ([ADR 0012](./docs/decisions/0012-llm-judge-dedup.md))
- [x] OpenAI provider for chat-model features (rerank / HyDE / dedup / summarizer)
- [x] **Session distillation writer (W8, [ADR 0020](./docs/decisions/0020-session-distillation-writer.md))** — `memstem hygiene distill-sessions [--backfill] [--apply]` produces `type: distillation` summaries with provenance back to source sessions. Shipped 0.9.0.
- [x] **Project records writer (W9, [ADR 0021](./docs/decisions/0021-project-records.md))** — `memstem hygiene project-records [--apply]` produces `type: project` rollups per Claude Code project tag with ≥2 sessions. Shipped 0.9.0.
- [x] HTTP API (the daemon hosts a 127.0.0.1 HTTP server alongside MCP; CLI delegates to it for sub-second queries)
- [ ] Apply-step for ADR 0012 dedup verdicts (audit log written; resolution still gated)
- [ ] Decay over time for importance + query-log driven boost tuning
- [ ] Anthropic `BetaAbstractMemoryTool` adapter ([ADR 0006](./docs/decisions/0006-anthropic-memory-tool-adapter.md))
- [ ] Backup: nightly `git push` to private remote
- [x] Test coverage at 88%
- [ ] Documentation site (memstem.com)

> **Removed from Phase 2 by [ADR 0019](./docs/decisions/0019-no-skill-authoring.md):** auto-skill extraction. Each AI generates skills its own way (Claude Code, Codex, Hermes, OpenClaw all have their own conventions); MemStem ingests `SKILL.md` files from disk but does not author them.

**Goal:** ready to publish as OSS. Repo flipped public 2026-04-30; v0.9.0 ("derived records") shipped 2026-05-01.

## Phase 3 — v0.3 (multi-AI breadth)

- [ ] Codex adapter
- [ ] Cursor adapter
- [ ] Aider adapter
- [ ] Hermes adapter
- [ ] Generic-filesystem adapter (point at any directory)
- [ ] Adapter SDK + docs for community adapters

**Goal:** the unified memory story is real for any AI stack.

## Phase 4 — v0.4 (collaboration features)

- [ ] Multi-device sync (CRDT-based or git-based)
- [ ] Per-tenant isolation
- [ ] Encryption at rest
- [ ] Audit log

## Phase 5 — v1.0 (managed offering, optional)

- [ ] Hosted Memstem (BYO embeddings, encrypted at rest, multi-region)
- [ ] Self-hosted enterprise tier
- [ ] B2B onboarding for multi-vendor AI shops

## Out of scope (probably forever)

- Becoming a chat application
- Becoming a wiki engine UI
- Replacing dedicated human note-taking apps

## Versioning

Pre-1.0: minor versions can break compatibility, patch versions can't. Post-1.0: SemVer.
