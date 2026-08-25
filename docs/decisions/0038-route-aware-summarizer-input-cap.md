# 0038 — Configurable summarizer input cap, self-backfilling on raise

Status: **Accepted — implemented**
Date: 2026-08-25
Related: 0020 (session distillation writer), 0037 (staleness-driven refresh)

## Context

`DEFAULT_MAX_INPUT_CHARS` (32,000 chars) was a hardcoded module
constant sized to the fleet's self-hosted Gemma backend, which serves
a 16,384-token context. Real-world measurement (user report,
2026-08-24, verified against the maintainer's vault) showed this is
not an edge-case bound: **46% of distilled sessions exceed it, 18%
exceed 100k chars**, so for day-scale working sessions the summary is
routinely generated from a head+tail slice that can miss mid-session
decisions entirely — even after an ADR 0037 refresh.

Meanwhile the fleet's sidecar v3 (MEMSTEM.md 2026-08-22) added a
cloud route serving a Gemma-class model with a **128k-token context
(~400k+ chars)**. A host whose *primary* route is the cloud model can
read all but freak-sized transcripts in a single pass. But the cap
must stay per-host: on a gx10-primary host an oversized prompt gets a
`400` from the 16k-context model, and the sidecar's failover triggers
on network errors/5xx — not 4xx — so a fleet-wide raise would break
distillation on every gx10-primary host.

The second problem is backfill: raising the cap does nothing for the
hundreds of existing summaries generated from truncated reads. A
separate backfill tool would duplicate the refresh machinery ADR 0037
just built.

## Decision

1. **The cap becomes config**: `hygiene.summarizer_max_input_chars`
   (default 32,000 — behavior unchanged until an operator raises it).
   Threaded through the daemon loop and exposed on the CLI as
   `--max-input-chars`. Raise it **only on hosts whose summarizer
   primary route reaches the large-context model.**

2. **Every distillation records how much it actually read**:
   `provenance.source_read_chars = min(len(transcript), cap)` at
   generation time, alongside the ADR 0037 snapshot fields.

3. **"Read less than we can now read" is a staleness condition.**
   `is_distillation_stale` gains: stale when
   `min(transcript_chars, current_cap) - source_read_chars >=
   REDISTILL_MIN_UNREAD_CHARS` (default 10,000). Legacy records with
   no `source_read_chars` are assumed to have read
   `min(their snapshot, 32,000)`.

   Consequence: **raising the cap is self-backfilling.** Every summary
   whose source was truncated harder than the new cap requires becomes
   an ordinary refresh candidate, drains through the normal hygiene
   cycles under `distill_max_per_cycle`, and converges (after the
   rewrite, `source_read_chars` equals the new bound, so the condition
   clears). No backfill tool, no flag day.

## Consequences

- With the cap unchanged, nothing changes: `min(len, 32k) − read ≤ 0`
  for every record, so the new condition never fires on a default
  host.
- Rollout is decoupled from the model-service cutover: raise the knob
  host-by-host as each host flips to the large-context primary route.
- Cost on a per-token cloud route scales with transcript size; the
  per-cycle cap remains the spend governor. The default cap keeps
  gx10-primary hosts exactly as they are.
- If the cloud route fails while a raised-cap host distills an
  oversized session, the local fallback model returns `400` and that
  session is skipped for the cycle (existing empty-summary handling);
  it retries when the route recovers.
- Freak transcripts beyond even the raised cap still truncate
  head+tail; chunked summarization stays future work, now needed only
  for the extreme tail (>0.5% of sessions).
- `project_records.DEFAULT_MAX_INPUT_CHARS` is unchanged in this ADR
  (project prompts aggregate several already-distilled inputs);
  extending the knob there is a follow-up.
