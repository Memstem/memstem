# ADR 0011: Write-time noise filter + atomic-fact extraction

Date: 2026-04-27
Status: Proposed

## Context

Adapters today read whole sessions and whole markdown files and persist
each as a single `Memory` record. On Brad's box that is ~940 records
on day one. By month three, with Claude Code + OpenClaw + Codex
ingesting continuously, projection is ~30k records, dominated by
heartbeat output, cron lines, boot-file echoes, and one-line transient
status messages.

The mem0 project audited 10,134 entries collected over 32 days and
found **97.8% were junk** (mem0 issue #4573). The taxonomy:

| % | Category |
|---|---|
| 52.7% | Boot-file restating (system prompts re-extracted every session) |
| 11.5% | Heartbeat / cron noise |
| 8.2%  | Architecture dumps |
| 7.4%  | Transient task state ("deploy by Friday") |
| 5.2%  | Hallucinated profiles |
| 3.3%  | Identity confusion (agent vs operator) |

A single hallucinated fact, when stored and re-extracted from recall
context, produced **808 duplicate copies**. mem0's own conclusion: *"A
better model follows the extraction prompt more faithfully, which
means it extracts more indiscriminately. The bottleneck isn't model
quality; it's the extraction pipeline architecture."*

MemStem stores raw session bodies verbatim today. Without a write-time
filter, the same junk taxonomy lands in our index. Cleaning it after
the fact is exponentially harder than refusing it at the door — every
downstream tier (importance scoring, distillations, dedup) wastes
cycles on garbage.

This ADR locks the v0.2 design for the ingest pipeline before any code
lands. It is independent of the AI client; the entire pipeline runs
inside MemStem and depends only on the files each adapter already
produces.

## Goals

1. Drop known-noise patterns deterministically at ingest, before
   anything reaches the index or the embedder.
2. Tag transient items with a TTL (`valid_to`) instead of persisting
   them indefinitely. Heartbeats and cron output expire after 4 weeks.
3. Split conversational sessions into atomic facts so dedup, importance
   scoring, and search operate on the right unit.
4. Preserve every raw session on disk as `type: session_raw`, excluded
   from default search but linkable from the extracted facts.
5. Stay local-first and cross-platform. Pure Python + Ollama; no API
   key required; runs identically on Linux, macOS, and (later) Windows.
6. Stay 100% inside MemStem. The thin redirect markdown in
   `~/.claude/CLAUDE.md` and `~/ari/openclaw.json` is the only touch
   point with Claude Code or OpenClaw — no hooks, no callbacks, no
   plugins on the client side.

## Non-goals

- Not a replacement for Tier 1 importance scoring (ADR 0008). The
  filter says *yes/no/transient*; importance ranks the *yes*.
- Not extracting from skill files (`SKILL.md`), already-curated memory
  files (`MEMORY.md`, `memory/*.md`), or instructions files. These
  already represent intentional human-or-LLM-curated state and pass
  through as `type: memory` records unchanged.
- Not deleting raw sessions. They become `type: session_raw` and stay
  on disk; only their default-search visibility changes.
- Not relying on a remote LLM. Optional API routing is allowed in
  config but is never the default — the install-time promise is
  "Ollama and you're done."

## Decision

Insert a new stage `core/extraction.py` between `Adapter` output and
`core/pipeline.upsert()`. The stage is two phases.

### Phase A — Heuristic noise filter (no LLM)

Pure regex / hash matchers; deterministic; runs on every adapter
record. Returns one of `KEEP`, `DROP`, `TAG_TRANSIENT(kind, ttl_days)`.

Initial taxonomy (extensible via `_meta/config.yaml`):

| Pattern kind          | Detector                                                                          | Action             |
|-----------------------|-----------------------------------------------------------------------------------|--------------------|
| `heartbeat`           | Body matches `HEARTBEAT_OK`, `[heartbeat]`, PM2 monitor preambles                 | `DROP`             |
| `cron_output`         | Body starts with cron-job markers (`__openclaw_*_dream__`, `* * * * *` echoes)    | `DROP`             |
| `boot_echo`           | First 1024 chars hash matches a known system-prompt file (CLAUDE.md / SOUL.md / MEMORY.md core block) | `DROP` |
| `tool_dump`           | >80% of body is uniform JSON / repeated tool-result blocks, no prose              | `DROP`             |
| `transient_task`      | Body contains time-bound markers (regex on `\b(today|tomorrow|by (Mon|Tue|...|Fri)day|this (PR|sprint|week))\b`) | `TAG_TRANSIENT('task', 28)` |
| `automation_log`      | Source path contains `agents/*/heartbeat/`, `monitoring/`, `pm2/logs/`            | `TAG_TRANSIENT('automation', 28)` |
| (default)             | None of the above                                                                 | `KEEP`             |

For `TAG_TRANSIENT`, the record is allowed through but its frontmatter
gains `valid_to: <ingest_time + ttl_days>`. ADR 0012's hygiene worker
later sweeps expired entries to the archive.

The boot-echo hash table is rebuilt at daemon start by hashing the
first 1024 bytes of every file matching `*/CLAUDE.md`, `*/MEMORY.md`,
`*/SOUL.md`, `*/USER.md`, etc., across all watched workspaces. This is
the cheapest guard against the 52.7% category in the mem0 audit.

### Phase B — LLM atomic-fact extraction

Runs on records that survive Phase A AND match either `type: session`
OR are over a configurable length threshold (default: 4000 chars). All
other records pass through unchanged.

For each qualifying record:

1. The original body is preserved as a sibling record with
   `type: session_raw` and frontmatter
   `excluded_from_default_search: true`. This stays linkable; nothing
   is destroyed.
2. The body is sent to a local Ollama model (default `qwen2.5:7b`,
   configurable) with the extraction prompt below.
3. Each line of the LLM output becomes its own `MemoryRecord` with
   `type: fact` and `extracted_from: <session_raw_id>`.
4. The `MemoryRecord` list — `[session_raw, fact_1, ..., fact_N]` —
   is handed to `core/pipeline.upsert()` as a batch.

If the LLM returns `NO_FACTS`, only the `session_raw` is written.

### Extraction prompt (canonical text)

Stored in `src/memstem/prompts/extract_facts.txt`:

```
You are a memory-extraction assistant. Extract durable, atomic facts
from the transcript below for an AI agent's long-term memory.

[IMPORTANT]: GENERATE FACTS SOLELY BASED ON THE USER'S MESSAGES. DO
NOT INCLUDE INFORMATION FROM ASSISTANT OR SYSTEM MESSAGES.

[IMPORTANT]: YOU WILL BE PENALIZED IF YOU INCLUDE INFORMATION FROM
ASSISTANT OR SYSTEM MESSAGES.

Each fact must:
- Be one self-contained sentence.
- Be durable. NEVER extract transient state ("deploying today",
  "running this build", "checking the logs now").
- Appear exactly ONCE. When the assistant restates, summarizes, or
  confirms information the user already provided in the same
  conversation, do NOT extract it again. (This is called "Echo
  Extraction" and produces duplicates.)
- Begin with a category in brackets, then the fact:
  [DECISION], [PREFERENCE], [PERSON], [PROJECT], [TECHNICAL],
  [BUSINESS], [RULE]

Output one fact per line. If the transcript contains no durable
facts, output exactly:
NO_FACTS

Transcript:
{transcript}
```

The two `[IMPORTANT]` blocks and the `NO_FACTS` sentinel are taken
verbatim from mem0's `ADDITIVE_EXTRACTION_PROMPT`. They are the only
documented mitigations for system-message bleed-through and Echo
Extraction. Do not paraphrase them when porting.

## Schema additions

All optional / additive. v0.1 readers ignore them.

### `type` enum gains two values

| Value         | Description                                                                  |
|---------------|------------------------------------------------------------------------------|
| `fact`        | Atomic durable fact extracted from a session by Phase B. Default search unit.|
| `session_raw` | Original session transcript, preserved for audit / re-extraction. Excluded from default search. |

### Frontmatter gains two fields

| Field                          | Type         | Description                                                  |
|--------------------------------|--------------|--------------------------------------------------------------|
| `extracted_from`               | id           | For `type: fact`, points to the `session_raw` record.        |
| `excluded_from_default_search` | bool         | True for `session_raw`. Searchable with `--include-archived`.|

(`valid_to` and `links` already exist from ADR 0008.)

## Pseudocode

```python
def extract(record: MemoryRecord) -> list[MemoryRecord]:
    decision = noise_filter(record)
    if decision.action == "DROP":
        return []
    if decision.action == "TAG_TRANSIENT":
        record.frontmatter.valid_to = (
            record.created + timedelta(days=decision.ttl_days)
        )

    needs_extraction = (
        record.type == "session"
        or len(record.body) >= EXTRACTION_LENGTH_THRESHOLD
    )
    if not needs_extraction:
        return [record]

    archived = record.copy(
        type="session_raw",
        excluded_from_default_search=True,
    )
    facts = llm_extract_facts(record.body)
    return [archived, *facts]
```

## Implementation phasing

Each PR is independently mergeable. Reverting any PR returns the
system to a strict subset of the previous behavior.

1. **PR-A: noise filter taxonomy + tests.** Phase A only. No LLM.
   Yields measurable disk and index reduction immediately.
2. **PR-B: TTL tagging for transient kinds.** `valid_to` writes; the
   hygiene worker (ADR 0012 / ADR 0008 Tier 3) sweeps later.
3. **PR-C: boot-echo hash table.** Computed at daemon start; refreshed
   when watched system-prompt files change.
4. **PR-D: LLM extraction stage + Ollama integration.** Phase B; gated
   behind `extraction.enabled: false` in config until tuned.
5. **PR-E: `session_raw` type + default-search filter.** Final wiring;
   the search layer learns to filter `excluded_from_default_search`.

PR-A and PR-B are <300 LOC each and can ship within a week. PR-D is
the longest at ~600 LOC including the prompt-runner, model warmup, and
batch handling.

## Rationale

- **Heuristics first because they're free.** A regex matcher costs
  microseconds. The mem0 audit's top three categories (52.7% boot
  echoes + 11.5% heartbeats + 7.4% transient state = 71.6%) are all
  pattern-detectable without an LLM. We refuse to pay LLM cost on
  obvious garbage.
- **Atomic facts because dedup needs atomic units.** ADR 0012's
  dedup pipeline cannot tell whether two 30-turn transcripts are
  duplicates. It can tell whether two single-sentence facts are. The
  unit of memory is the unit of dedup.
- **Preserve raw sessions because audit matters.** "What did the user
  literally say on Tuesday?" is a real question, especially for
  decisions made under unusual context. Throwing the transcript away
  to save space is the wrong tradeoff. Disk is cheap; the
  `session_raw` records sit in the archive and never load into
  default queries.
- **Ollama because cross-platform.** Linux, macOS (Apple Silicon and
  Intel), and Windows all run Ollama natively. No API keys, no
  network round-trips, no per-extraction cost. The latency
  (2-5s/session) is acceptable for an async ingest path.
- **mem0's exact prompt because they paid the price.** The double
  `[IMPORTANT]` block and the Echo Extraction rule are not stylistic
  choices. They are documented mitigations for the exact failure
  modes that produced 808 duplicate copies of one hallucinated fact.
  Porting verbatim is cheaper than re-discovering.

## Consequences

**Pros:**

- Eliminates an estimated 70-90% of incoming noise at ingest, before
  it consumes embedding cycles or index space.
- Atomic facts give dedup, importance, and search the right unit.
- TTL on transient items means heartbeats expire automatically without
  user intervention.
- 100% MemStem-internal. No client-side hooks; nothing in Claude Code
  or OpenClaw to break across their updates.
- Cross-platform from day one.

**Cons:**

- LLM extraction adds 2-5 seconds of latency per qualifying session.
  Mitigation: ingest runs async; UI never waits on it.
- Extraction quality is bounded by `qwen2.5:7b`. Bad facts can leak.
  Mitigation: ADR 0012's dedup judge will catch obvious echoes; user
  can re-run extraction with a stronger model later via `memstem
  reextract <session_id>`.
- New schema types (`fact`, `session_raw`) and frontmatter fields are
  long-term commitments once written to user vaults. Mitigation: both
  are additive; v0.1 readers ignore unknown fields and unknown enum
  values fall back to `memory`.
- Heuristic patterns will need tuning as new noise categories appear.
  Mitigation: pattern table is config, not code; users can add their
  own.

## Open questions

- For Phase B, should very short sessions (< 4000 chars but > 5 turns)
  also go through extraction? Current draft: no — they're cheap to
  store raw and probably already atomic.
- For the LLM model default: stick with `qwen2.5:7b` or move to
  `llama3.3:8b` once it's stable in Ollama? Decide in PR-D.
- Should the extraction prompt include the session's title/path as
  context? Risk: model treats path as instruction. Current draft: no,
  body only.
- For multi-agent OpenClaw workspaces, do facts inherit the
  `agent:<tag>` tag from the source session, or get tagged
  `extracted` only? Current draft: inherit (preserves agent
  isolation in search).

## References

- mem0 audit: https://github.com/mem0ai/mem0/issues/4573
- mem0 prompts: https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py
- ADR 0002 (markdown-canonical invariant — `session_raw` preserves it)
- ADR 0005 (pull-based ingestion — extraction stays adapter-agnostic)
- ADR 0008 (Tier 1 importance scoring — consumes `type: fact` records)
- ADR 0012 (dedup pipeline — operates on the atomic facts this stage produces)
