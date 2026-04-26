# ADR 0009: Pluggable embedder backends + always-on embed queue

Date: 2026-04-26
Status: Accepted

## Context

Phase 1 v0.1 shipped with a single embedder (Ollama with
`nomic-embed-text`) and inline embedding inside `pipeline.process`.
Both choices held up architecturally but broke down in practice during
the live cutover on Brad's box:

1. **Inline embedding couples ingest latency to embed throughput.** A
   bulk migrate of 1044 records on CPU-only Ollama hit 30s per-request
   timeouts (the runner queued requests serially), grinding to ~5
   records/min. Records still landed in vault + FTS5 but their vectors
   didn't. Hours of wall time, partial vectors, no progress feedback.
2. **A single embedder backend is a lock-in.** Ollama is correct for
   the local-first promise (ADR 0001 / 0003) but local CPU embedding
   is fundamentally slow. Users with API budgets — or their own
   GPU-backed LLM endpoints (vLLM, LM Studio) — should be able to
   point Memstem at those. ADR 0003 listed Ollama as the chosen
   embedder, but didn't preclude alternates.
3. **No retry surface.** When an embed call failed, the record was
   left without a vector and the pipeline moved on. There was no
   queue, no retry, no visibility — just a `WARNING` log line and
   degraded retrieval forever.

PR #24 partially addressed (1) with `migrate --no-embed` and a 30 →
120 s default timeout, but those were workarounds layered on the
broken default rather than a fix.

## Decision

Two architectural changes, landed together as PR #26:

### 1. Pluggable embedder backends

A formal `Embedder` ABC in `memstem.core.embeddings` with four
shipped implementations:

| Class | Backend | Default model | API key env |
|---|---|---|---|
| `OllamaEmbedder` | Local Ollama (default) | `nomic-embed-text` (768d) | none |
| `OpenAIEmbedder` | OpenAI + any OpenAI-compatible (`base_url` knob: Together, Mistral, Groq, vLLM, LM Studio, ...) | `text-embedding-3-small` (1536d) | `OPENAI_API_KEY` |
| `GeminiEmbedder` | Google's Generative Language API | `text-embedding-004` (768d) | `GOOGLE_API_KEY` |
| `VoyageEmbedder` | Voyage AI (Anthropic-recommended partner) | `voyage-3` (1024d) | `VOYAGE_API_KEY` |

`embed_for(EmbeddingConfig)` is the factory. The user picks a
provider in `_meta/config.yaml`; only that provider's API key needs
to exist. **API keys live in environment variables**, not in the
vault — config names the env var (`api_key_env: OPENAI_API_KEY` etc.)
and the embedder reads it at instantiation. The vault stays clean
enough to back up to a public repo.

Default is unchanged: **Ollama, local-first, no API key required**.
The local-first story was the original differentiator and remains so.
API options are opt-in via a one-line config edit.

### 2. Always-on embed queue

Embedding moves out of `pipeline.process` and into a dedicated worker
that drains a SQLite-backed queue (`embed_queue` table; schema
migration v2). The pipeline becomes fast-path only:

```text
adapter   →   pipeline.process   →   vault + memories + FTS5 + tags + links
                                  ↓
                              embed_queue.enqueue(memory_id)
                                  ↑
                       EmbedWorker drains, calls embedder,
                       writes memories_vec, dequeues
```

Properties:

- **Ingest latency is bounded by disk + SQLite**, never by embedding.
  A migrate of 1000 records writes vault + FTS5 in seconds; the
  embedder catches up on its own schedule.
- **Retries with backoff.** Failed embeds increment `retry_count`
  and stay in queue. After `max_retries` (default 5), the row flips
  to `failed=1` and is skipped. Editing the record (or
  `memstem embed --retry-failed`) resets it.
- **Visibility.** `memstem doctor` reports `Embed queue: N pending,
  M failed`. Operators can tell whether the queue is keeping up.
- **Concurrency** is task-level: `EmbeddingConfig.workers` (default
  2) controls how many async tasks share the queue. SQLite's
  serialization handles writer contention; the workers naturally
  pick different `memory_id`s without explicit locking. CPU Ollama
  is happiest at `workers=1`; API providers tolerate 4+ but watch
  for rate limits.
- **Two ways to drain.** `memstem daemon` runs the worker
  continuously alongside the watch loop. `memstem embed` is a
  one-shot drain (returns when the queue is empty) for manual
  catch-up after a `migrate` or a provider switch.

`migrate --no-embed` becomes a no-op alias since embedding is always
deferred. `install.sh --migrate-no-embed` is similarly preserved as
an alias for back-compat with PR #23/#24 invocations.

## Rationale

### Why a queue, not async embedding-during-process?

A bounded async pool inside `pipeline.process` (await an embed slot,
then continue) would solve the "blocks ingest" half of the problem
but not the "no retry, no visibility" half. A persistent queue gives
us:

- **Crash recovery for free.** Process exits, daemon restarts, queue
  picks up where it left off — even mid-record.
- **Provider switches don't lose data.** Change provider config,
  enqueue everything via `memstem reindex`, drain at the new
  provider's pace. No "embed during walk" rewrite.
- **A natural place to add hygiene.** ADR 0008's tiered-memory work
  needs a way to enqueue distillation requests without blocking
  ingest. Same queue pattern, different worker.

### Why provider config in YAML rather than per-environment?

We considered keeping provider as an env var (`MEMSTEM_EMBED_PROVIDER=gemini`)
to avoid re-running `init`. Decided against: provider choice is
vault-bound (vectors from one provider are gibberish to another), and
multiple vaults on one machine want independent providers. YAML lives
with the vault; env-only config would conflict with that boundary.

### Why not just ship Voyage as the default?

Voyage tops MTEB benchmarks and Anthropic recommends it. But making
it the default would:

1. Break the local-first promise (ADR 0001) — a fresh install would
   need an API key before it could embed anything.
2. Coupling Memstem to a single hosted provider runs counter to the
   pull-based, hooks-free architecture that gives the project its
   identity. Memstem is "your memory, indexed however you want it";
   not "your memory, on Voyage".

Ollama default + Voyage easy-opt-in is the right shape.

### Why not just batch chunks across records?

The pipeline already batches chunks within a single record (Ollama
`/api/embed` accepts a list). Cross-record batching would help
throughput but wouldn't fix the structural problems (latency
coupling, no retry, no visibility). The queue subsumes this — a
worker can pull N records, batch their chunks, and send one big
request when the backend supports it.

## Consequences

### Pros

- Fresh installs come up in seconds even with thousands of records:
  vault + FTS5 land synchronously, vectors backfill behind.
- Operators can switch embedding providers without code changes —
  one YAML edit + `memstem reindex`.
- Cost-conscious users stay on Ollama; speed-conscious users move
  to Gemini ($0.025/1M) or Voyage ($0.06/1M); GPU-Ollama users get
  the best of both.
- Crash recovery is free.
- ADR 0008's hygiene worker has a place to enqueue distillation
  jobs without inventing a second queue.

### Cons

- One more SQLite table to maintain. Migration v1 → v2 is automatic.
- Worker is a long-running task — daemon now has three concurrent
  jobs (reconcile, watch, embed) instead of two. `memstem embed`
  command provides a way to drain without the full daemon.
- Provider switches require a `memstem reindex` (well-known caveat,
  documented in README).
- API providers introduce dependencies on third-party services for
  users who opt in. Local Ollama remains the default mitigation.

## Alternatives considered

- **Async embedder pool inside `pipeline.process`.** Cleaner code,
  no queue table. Rejected: doesn't survive restarts, doesn't allow
  retry, doesn't help provider switches.
- **fastembed / ONNX in-process embedder.** Faster than HTTP-Ollama
  on CPU. Rejected for v0.1: bigger dependency footprint, conflicts
  with the "Ollama is the embedder" choice in ADR 0003. Could be
  added as a fifth pluggable backend in v0.2 if demand exists.
- **Switching default to a hosted API.** Discussed and rejected
  above on local-first grounds.
- **Anthropic-hosted embeddings.** Anthropic doesn't offer a hosted
  embedding model; Voyage is their recommended partner and is
  shipped here as the Anthropic-aligned choice.

## References

- ADR 0001 — local-first design
- ADR 0002 — markdown canonical / index rebuildable
- ADR 0003 — SQLite + FTS5 + sqlite-vec, Ollama as the v0.1 embedder
- ADR 0008 — tiered memory (proposed); will share the queue pattern
- PR #24 — interim throughput fixes (`--no-embed`, timeout bump,
  progress reporting). Superseded by this ADR's queue.
- PR #25 — agent-scoped paths + orphan-row cleanup. Independent.
