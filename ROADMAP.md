# Roadmap

## Phase 0 — Foundations (current)

- [x] Repo scaffold, docs, CI
- [ ] Frontmatter spec finalized
- [ ] MCP tool definitions finalized
- [ ] Adapter interface defined

## Phase 1 — v0.1 (single-user, local, working)

- [ ] Markdown canonical storage layer (read/write/walk)
- [ ] SQLite index with FTS5 + sqlite-vec
- [ ] Embedding integration via Ollama (nomic-embed-text)
- [ ] Hybrid search with RRF
- [ ] Claude Code adapter (session JSONL watcher)
- [ ] OpenClaw adapter (memory dir watcher)
- [ ] MCP server with `search`, `get`, `list_skills`
- [ ] CLI: `memstem init`, `daemon`, `search`, `reindex`
- [ ] Migration script: import existing `~/ari/memory/` + Claude Code sessions

**Goal:** running locally, replacing FlipClaw end-to-end. Not yet public.

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
