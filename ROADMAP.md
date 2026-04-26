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

## Phase 2 — v0.2 (production-ready single-user)

- [ ] Hygiene worker: dedup + staleness scoring
- [ ] Auto-skill extraction
- [ ] Anthropic `BetaAbstractMemoryTool` adapter
- [ ] HTTP API
- [ ] Obsidian-vault-compatibility audit (wikilinks parser, OFM dialect)
- [ ] Backup: nightly `git push` to private remote
- [ ] Test coverage > 80%
- [ ] Documentation site

**Goal:** ready to publish as OSS. Repo flips public.

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
- Replacing Obsidian/Logseq for human note-taking

## Versioning

Pre-1.0: minor versions can break compatibility, patch versions can't. Post-1.0: SemVer.
