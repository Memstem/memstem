# 0030 — Embed-client resilience: short interactive query timeout + circuit breaker

Status: **Accepted**
Date: 2026-07-07
Supersedes: none
Related: 0025 (query/document embedding asymmetry), search hybrid fusion (BM25 fallback)

## Context

The embedder is reached over the network (self-hosted vLLM behind a Cloudflare
tunnel). When that path has a transient bad window, two design choices in the embed
client turned a brief blip into a prolonged, fleet-visible outage — the amplifier behind
the 2026-07 embed-storm incident:

1. **One 120s timeout for everything.** `DEFAULT_TIMEOUT = 120s` was hardcoded and the
   `EmbeddingConfig.timeout` knob was never wired through. The **interactive** search
   query-embed (`Search.query_bm25`/`query_vec` path) used the same 120s client as
   background document embedding. So when the embedder stalled, a *search* hung up to
   120s before falling back to BM25 — a 1-second hiccup became a 30–120s search hang for
   every user query.
2. **Unbounded transient retries.** Background embed workers retry `TransientEmbeddingError`
   (5xx/network/timeout) without a ceiling. During a sustained bad window they keep
   hammering the struggling endpoint, which inflates error counts (hundreds of thousands
   of cloudflared origin errors were observed) and *slows recovery* by keeping the
   endpoint under load.

The endpoint (vLLM) and the tunnel are healthy at baseline; the problem is that the
client does not **degrade gracefully** when the endpoint is briefly unreachable.

## Decision

Make the embed client tolerate transient endpoint unavailability, so a blip is invisible
instead of a crisis. Two mechanisms, both configurable, both off-path when healthy.

### 1. Separate, short timeout for interactive query embedding

- `EmbeddingConfig.timeout` (default **120s**) — background/document embedding. Generous:
  a slow-but-working backend under bulk ingest must not fail records.
- `EmbeddingConfig.query_timeout` (default **5s**) — interactive search-query embedding.
  Short on purpose: a search must degrade to BM25 in seconds, not hang.

The base `Embedder` threads a per-request `timeout` into every provider's `_embed_batch`
(renamed from `embed_batch`; the public `embed_batch(texts)` is now a base wrapper that
uses `timeout`). `embed_query` uses `query_timeout`; `embed`/`embed_batch` use `timeout`.
`embed_for` wires both from config. The search path already falls back to BM25 on any
embed exception, so a fast query-timeout makes that fallback fast.

### 2. Circuit breaker (shared per embedder instance)

- After `circuit_breaker_failures` (default **4**) consecutive `TransientEmbeddingError`s,
  the circuit **opens** for `circuit_breaker_cooldown_s` (default **30s**). While open,
  `embed_query` / `embed_batch` raise `TransientEmbeddingError` immediately — search →
  BM25 instantly, workers back off — without touching the network. A success (or a
  permanent `EmbeddingError`, which means the endpoint answered) resets the streak.
- `circuit_breaker_failures = 0` disables the breaker. The breaker is instance-shared
  (one endpoint → one circuit), state is created lazily (no `__init__` change in
  providers), and updates are lock-guarded for the concurrent workers.

Permanent errors (4xx / bad input) do **not** trip the breaker — the endpoint is reachable,
it just rejected the input.

## Consequences

- A transient embedder blip degrades search to BM25 in ≤ `query_timeout` (≤5s) for the
  first affected query, and instantly for the rest once the breaker opens — instead of
  30–120s hangs across the fleet.
- The breaker stops the retry storm, cutting error amplification and letting the endpoint
  recover under less load.
- All defaults preserve prior behavior for healthy endpoints (breaker never trips; doc
  timeout unchanged at 120s). Tunable per-vault via config.
- Not a substitute for fixing the *trigger* (endpoint capacity/headroom) — it makes the
  client robust to triggers that will always occasionally happen on a networked backend.
