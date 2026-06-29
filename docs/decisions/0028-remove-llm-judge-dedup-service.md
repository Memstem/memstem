# 0028 — Remove the LLM-judge dedup service (keep Layer-1 exact-hash dedup)

Status: **Accepted — implemented**
Date: 2026-06-29
Supersedes: 0012 (two-stage dedup with LLM-as-judge)
Related: 0008 (tiered memory), 0023 (in-daemon hygiene loop)

## Context

ADR 0012 specified a three-layer dedup design:

- **Layer 1** — write-time exact dedup. Normalize the body, SHA-256 it, and
  on a hash collision increment a `seen_count` instead of writing a second
  copy. Cheap, local, deterministic, no model calls.
- **Layer 2** — semantic near-duplicate *candidate generation*: an O(N²) vec
  k-NN walk over the index to surface pairs above a cosine threshold.
- **Layer 3** — an LLM-as-judge (Gemma / OpenAI-compatible) that classifies
  each candidate pair (DUPLICATE / CONTRADICTION / RELATED / UNRELATED) and
  writes verdicts to a `dedup_audit` table.

Layers 2–3 shipped as a hygiene-loop stage (`dedup_judge`) plus two CLI
subcommands (`hygiene dedup-candidates`, `hygiene dedup-judge`), gated behind
config (`judge_provider`, `dedup_interval_seconds`, …) and run on an interval
by the in-daemon loop and a weekly cron.

In practice the Layer-2/3 service proved **unnecessary and complicating**:

- It was the heaviest intermittent job in the system — the O(N²) candidate
  walk plus per-pair LLM calls — and it contended with the shared GPU backend
  (the same box that serves distillation/summarization), causing intermittent
  contention with no offsetting benefit.
- Its output sat in an audit table that nothing acted on automatically
  (verdicts were never auto-applied), so the operational cost bought
  inventory, not cleanup.
- Layer 1 already prevents the high-volume failure mode it was meant to
  address (the hallucination-feedback exact-duplicate explosion).

## Decision

**Remove the Layer-2/3 LLM-judge dedup service. Keep Layer-1 exact-hash dedup.**

Removed:

- `hygiene/dedup_candidates.py`, `hygiene/dedup_judge.py`,
  `prompts/dedup_judge.txt`.
- The `dedup_judge` hygiene-loop stage (`STAGE_DEDUP_JUDGE`, `_run_dedup_judge`,
  `_get_judge`) and its state-table constant.
- The `hygiene dedup-candidates` and `hygiene dedup-judge` CLI subcommands.
- The service-only config fields: `dedup_interval_seconds`,
  `dedup_max_per_cycle`, `dedup_max_outer_memories`, `dedup_threshold`, and the
  `judge_*` block (`judge_provider`, `judge_model`, `judge_base_url`,
  `judge_api_key_env`).
- The tests targeting the service.

Kept (explicitly):

- **Layer 1** — `core/dedup.py`, the body-hash check in `core/pipeline.py`
  (`normalized_body_hash` / `find_memory_id_for_body_hash` /
  `increment_seen_count`), and the index method behind it. This remains the
  project's dedup mechanism.
- The `hygiene cleanup-retro --dedup/--no-dedup` retro body-hash pass (Layer 1).
- The `dedup_audit` table definition in `core/index.py`. Dropping it would be a
  schema migration with no benefit; an unused table is harmless. We simply stop
  writing service rows to it.
- The cleanup-retro collision dedup in `hygiene/cleanup_retro.py` and the
  `active_dedup_*` reporting in `hygiene/verify.py` — these are Layer-1 /
  retro-cleanup, not the LLM-judge service.

## Consequences

- The in-daemon hygiene loop now runs three stages (`importance`,
  `distill_sessions`, `project_records`); `/health` reports those.
- An existing `config.yaml` that still carries the removed `dedup_*` / `judge_*`
  keys continues to load unchanged — pydantic ignores unknown keys
  (`extra='ignore'`), so no migration is forced on operators. The stale keys
  are simply inert and can be deleted at leisure.
- The weekly dedup cron and the local `memstem-dedup-weekly.sh` wrapper become
  no-ops for the removed subcommands; they are decommissioned separately
  (operational change, outside this repo).
- No change to retrieval, storage canonicality, or the adapter interface.
