# ADR 0002: Markdown files as canonical storage

Date: 2026-04-25
Status: Accepted

## Context

Memstem needs a canonical store for memories and skills. Options considered:

- Pure SQLite (no flat files)
- Pure vector database (Qdrant, Chroma)
- Markdown files + derived index
- Custom binary format

## Decision

Markdown files in a structured directory tree are the canonical layer. A SQLite database (FTS5 + sqlite-vec) is a derived, rebuildable index.

## Rationale

1. **Durability across tool churn.** If the Memstem binary disappears tomorrow, every memory is still grep-able and human-readable. No proprietary format to lose.
2. **Diffable, git-friendly.** Memory state can be version-controlled and reviewed.
3. **Tool ecosystem.** Markdown + frontmatter is supported by Obsidian, Logseq, vim, every editor, every renderer. Free human view.
4. **Indexes can be regenerated.** A corrupt or incompatible index is a 5-minute `memstem reindex` away from healthy.
5. **Karpathy convergence.** The 2026 consensus pattern (Karpathy LLM Wiki, basic-memory, memweave) is markdown-canonical.

## Consequences

**Pros:** durability, portability, human-inspectability, Obsidian compatibility comes free.

**Cons:** writes touch the filesystem and the index (slightly slower than DB-only). Mitigated by async writes and batched index updates.
