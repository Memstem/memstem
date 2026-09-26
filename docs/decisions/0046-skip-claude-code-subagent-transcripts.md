# 0046 — Skip Claude Code subagent transcripts

- Status: accepted
- Date: 2026-09-26
- Amends: the claude-code adapter's session ingestion (ADR 0034 project tags)

## Context

Claude Code writes each subagent's transcript under
`<encoded-cwd>/<session>/subagents/agent-<id>.jsonl`, and workflow agents
under `.../subagents/workflows/wf_<id>/` (older builds used
`<encoded-cwd>/wf_<id>/`). Every entry in those files carries the
**parent's** `sessionId`. The adapter used that as the record's
`session_id`, so the pipeline placed the parent and all of its subagents at
the same vault path, `sessions/<parent>.md`.

`Index.upsert` treats a different memory id at an occupied path as a
displacement: it deletes the previous record's memories row, tags, FTS,
vectors and (by cascade) `embed_state` and `body_hash_index`, and the vault
file is overwritten. Each time the parent or a subagent re-emitted (every
startup and periodic reconcile, and live while a subagent runs), the winner
was embedded from scratch and the loser's vectors became dead vec0 slots.
Because the loser's hash row was gone, the reconcile skip never applied, so
the fight repeated on every pass. The canonical markdown for a session could
hold a subagent's transcript instead of the session's own.

Measured on brads-server, 2026-09-26: 743 subagent transcripts (368 MB)
beside 801 main sessions, 597 `record_map` entries pointing at displaced
memories, and 55 sessions (2,705 chunks) fully re-embedded within two
minutes of a restart. Per-chunk reuse (ADR 0045) cannot help, because the
memory id changes on every swap.

## Decision

The claude-code adapter skips subagent and workflow-agent transcripts: any
file under a `subagents` or `wf_*` directory, and any transcript whose
entries are marked `isSidechain: true`. They are skipped in the reconcile
walk, in live watch events and in parsing.

The parent transcript already contains each subagent's final report (the
Task tool result), which is the part worth recalling. Indexing the
transcripts separately was considered and rejected by the maintainer. It
would add roughly 180K vectors to a 212K-vector index, slowing every
brute-force scan proportionally, and would cost a one-time embedding bill
for mostly intermediate tool output.

Existing records are cleaned by `_prune_subagent_records`, run after every
startup and periodic reconcile. It removes each claude-code `record_map` row
whose ref is a subagent transcript. It removes the memory the row points to
only if no other ref keeps it. It deletes the vault file only when that
file's own frontmatter names both this memory and a subagent transcript, so
a parent that has reclaimed the path is never touched. The parent's next
reconcile rewrites `sessions/<id>.md` from its own transcript.

## Consequences

- Subagent internals are no longer searchable. Their outcomes are, through
  the parent session.
- Sessions stop swapping identity, so re-embeds become rare, and those that
  do happen go through ADR 0045's in-place path.
- If a parent's `.jsonl` has already been deleted upstream (Claude Code
  prunes old transcripts), its vault file may still hold a subagent's
  transcript, and the cleanup removes it. That session's own text was
  already lost in the vault by the time of this change.

## Addendum (2026-09-26, 0.25.2): Codex subagent threads

Codex has the same collision. Each spawned agent's thread is its own
rollout, whose **first** `session_meta` carries the thread's own `id`,
`thread_source: "subagent"` and `parent_thread_id`. The rollout then
replays the parent's history, including the parent's `session_meta`. The
adapter kept the *last* `session_meta` id, so the thread was filed under
the parent's `sessions/<id>.md`. On brads-server: 603 subagent rollouts
(427 MB) out of 2,785; 394 of them owned a live memory; every restart
re-processed ~800 Codex records, and one restart dropped ~9K vectors.

The Codex adapter now takes the rollout's identity (`id`, `cwd`, CLI
version, provider) from the first `session_meta` only, and skips rollouts
whose first `session_meta` says `thread_source: "subagent"`. User forks
keep their own id and are still ingested. `_prune_subagent_records` also
covers Codex refs (`is_subagent_rollout` reads up to the first
`session_meta`; a missing or unreadable file is never treated as a
subagent). A vault file is deleted only when its frontmatter names that
memory and that exact ref.
