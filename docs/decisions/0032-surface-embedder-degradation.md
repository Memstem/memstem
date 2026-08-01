# 0032 — Surface embedder degradation in search results

Status: **Accepted**
Date: 2026-07-11
Supersedes: none
Related: 0030 (embed-client resilience), 0031 (daemon persists resolved embedder key)

## Context

When the query-embed step fails during a hybrid search — auth failure (401 from
a stale key), connection error, timeout, open circuit breaker — `Search`
falls back to keyword-only BM25 retrieval. That fallback is the right
*availability* call (ADR 0030 leans on it: search must never go mute), but it
was **silent** to every consumer: the only trace was a
`vec query failed; falling back to BM25` warning in a log stream nobody
watches at MCP-spawn time.

In the 2026-07 stale-`secrets.yaml` incident, every cold-spawned
`memstem mcp` process served keyword-only results for weeks. Agents got
plausible-looking hit lists with quietly reduced semantic recall; nothing in
the payload said "this is the degraded path." Nobody noticed until recall
complaints were investigated manually.

## Decision

**Make the fallback visible at every surface, without breaking the payload
shape.**

1. **Internal:** `Search.search_with_status(...)` returns a `SearchOutcome`
   dataclass — `results: list[Result]`, `degraded: bool`,
   `degraded_reason: str | None`. The historical `Search.search(...)` is now a
   thin wrapper returning `.results`, so every existing caller keeps its
   `list[Result]` contract. `degraded` is `True` only when an embedder was
   *configured* and the vec query raised; an embedder-less vault is not
   degraded — BM25-only is its normal mode. The existing log warning is
   preserved unchanged.

2. **MCP `memstem_search` and HTTP `POST /search`:** each hit gains an
   additive `embedder_degraded: bool` field (default `false`). Per-hit rather
   than a response envelope because both surfaces return a bare list of hits;
   wrapping the list in an object would break every existing consumer, while
   an extra key on each hit is ignored by consumers that don't know it
   (the daemon client explicitly tolerates unknown fields).

3. **CLI:** `memstem search` prints a one-line notice to **stderr** when the
   call was degraded (both the daemon-delegated and direct-DB paths), pointing
   at `memstem doctor embedder`. Stderr so scripts parsing stdout see no
   change.

4. **Client:** `memstem.client.SearchHit` gains `embedder_degraded: bool =
   False`, parsed defensively so payloads from older daemons (field absent)
   still parse.

## Alternatives considered

- **Response envelope** (`{"degraded": ..., "results": [...]}`): the honest
  shape, but a breaking change to the MCP tool result and the HTTP response
  model; every consumer (OpenClaw bundles, scripts, the CLI client) would need
  a lockstep upgrade. Rejected for now; a v2 envelope can subsume the per-hit
  flag later.
- **Raise instead of falling back:** contradicts ADR 0030 and the "daemon
  never goes mute" invariant. Degraded results are far better than no results.
- **Only fix detection out-of-band** (`memstem doctor embedder`, ADR 0031
  self-heal): those prevent and diagnose the incident class, but an agent
  reading search results still couldn't tell it was on the degraded path
  *right now*. Defense in depth wants the in-band signal too.

## Consequences

- Agents and humans can see "results are keyword-only because the embedder is
  unreachable" in the same payload as the results, and react (retry, alert,
  run the doctor) instead of quietly acting on thin recall.
- Known limitation of the per-hit encoding: a degraded search with **zero**
  hits has nowhere to carry the flag. Acceptable — the empty-and-degraded
  case is rarer, the CLI stderr notice still fires, and the envelope
  redesign can fix it properly if it ever matters.
- One redundant boolean per hit on the wire (~25 bytes). Negligible.
- New public API surface: `SearchOutcome` and `Search.search_with_status`
  are exported and covered by tests; `search()` semantics are unchanged.
