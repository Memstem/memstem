# ADR 0003: SQLite with FTS5 + sqlite-vec for the index

Date: 2026-04-25
Status: Accepted

## Context

The derived index needs hybrid search (keyword + semantic). Options:

- pgvector (requires Postgres server)
- Qdrant / Chroma / Weaviate (separate vector service)
- LanceDB (purpose-built embedded vector DB)
- DuckDB with vector extension
- SQLite with FTS5 (built-in) and sqlite-vec (extension)

## Decision

SQLite with FTS5 (built-in) for keyword/BM25 + sqlite-vec extension for vector similarity. Single `index.db` file per vault.

## Rationale

1. **Stability across versions.** SQLite is the most-deployed software in history; format and API stability are world-class.
2. **No daemon dependency.** Embedded; no separate server to install or maintain.
3. **Hybrid in one query.** FTS5 + sqlite-vec results merged via RRF in pure SQL.
4. **Performance is sufficient.** Reports of <1ms over 4,300 memories vs Pinecone p95 25-50ms.
5. **Local-first by default.** No cloud dependency, no API keys.

## Consequences

**Pros:** simplicity, durability, single-file index, fast.

**Cons:** sqlite-vec is younger than alternatives (newer than sqlite-vss, written by same author). API may evolve.

**Mitigation:** abstraction in `core/index.py` keeps the choice swappable.
