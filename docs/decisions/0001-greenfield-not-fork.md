# ADR 0001: Greenfield, not a doobidoo fork

Date: 2026-04-25
Status: Accepted

## Context

The closest existing project to Memstem is `doobidoo/mcp-memory-service` — Apache 2.0, sqlite-vec + BM25 hybrid retrieval, MCP-native, with an `X-Agent-ID` multi-client tagging pattern. We considered forking it as a faster path to a working v0.1.

## Decision

Build greenfield. Reference doobidoo's design (especially the multi-client tagging) but do not fork.

## Consequences

**Pros:**

- Clean repo ownership and license posture (MIT, our copyright)
- No upstream attribution, NOTICE, or change-tracking obligations
- Freedom to shape the codebase around the multi-AI pull architecture (which doobidoo doesn't have)
- Simpler contributor onboarding (no inherited abstractions)

**Cons:**

- More upfront engineering work (~3-4 weeks vs ~1 week to v0.1)
- Re-implementing well-trodden patterns (sqlite-vec setup, FTS5 + RRF)

**Mitigations:**

- Borrow specific patterns aggressively (multi-client provenance, tag schemas, MCP shape)
- Use Alex Garcia's reference sqlite-vec/FTS5 hybrid implementation as a guide
