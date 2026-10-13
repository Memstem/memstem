# 0053 — Drop every context block Codex injects into user turns

Status: **Accepted**
Date: 2026-10-13
Related: ADR 0022 (Codex adapter), ADR 0037 (distillation staleness), ADR 0045 (per-chunk embedding reuse)

## Context

ADR 0022 drops Codex boilerplate in two places: `developer`-role messages, and a user
message that *starts with* `<environment_context>`. Newer Codex releases put more of
their own context into the first user message, each piece as a separate `input_text`
block:

| Block | Typical size |
|---|---|
| `# AGENTS.md instructions for <cwd>` + `<INSTRUCTIONS>…</INSTRUCTIONS>` | 4–35 KB (the whole AGENTS.md) |
| `<recommended_plugins>…</recommended_plugins>` | 1–3 KB (the uninstalled-plugin catalogue) |
| `<environment_context>…</environment_context>` | ~0.5 KB |
| `<skill><name>…</name>…</skill>` (when a skill is invoked) | the whole SKILL.md |

The adapter joined the blocks of a message and tested only the joined text's prefix, so
everything after the first block was kept as if the user had typed it, and the first
block often became the session title (`<recommended_plugins>`). Measured on the managed
fleet on 2026-10-13, this injected text was 90–91% of all indexed text on two agent
vaults, 76% on a third, 66% on a headless-agent vault and 11% on an operator vault.
Every search scanned those vectors, and the distiller's capped read of a session was
mostly AGENTS.md rather than the conversation.

## Decision

Filter user turns per content block, before joining:

- Drop blocks starting with `<environment_context>`, `<permissions instructions>`,
  `<recommended_plugins>` or `<user_instructions>` (the pre-AGENTS.md wrapper).
- Drop a block that is `# AGENTS.md instructions…` followed by `<INSTRUCTIONS>`. The
  tag requirement keeps a real message that merely begins with those words.
- Replace an invoked `<skill>` block with `[skill: <name>]`. The skill is indexed on its
  own; that it was used is part of the conversation.
- Assistant text is never filtered. Text that quotes a tag mid-message is kept.

A message left empty is dropped like any other empty turn, so the title comes from the
first real user text.

## Consequences

- Existing sessions are rewritten by the ordinary startup reconcile: the parsed body
  differs, so the body-hash skip (ADR 0024) falls through to `Pipeline.process`, which
  updates the record in place under the same `(source, ref)` id and re-embeds it. No
  migration command is needed while the rollout files are on disk.
- Distillations are not regenerated: the ADR 0037 staleness gate fires on growth, and
  these bodies shrink. Refreshing summaries written from boilerplate is a separate,
  opt-in backfill.
- Old vectors become dead slots; the `vec_compact` hygiene stage reclaims them once the
  dead fraction passes its threshold.
- A rollout whose only user content was injected text now yields no record; its earlier
  record is left as it was.

## Addendum (0.31.1) — retire a session that now duplicates another

Rolling 0.31.0 out on a headless-agent vault showed that once the injected text is
gone, thousands of scheduled runs are byte-identical. ADR 0012's exact-body dedup then
returned early for each later copy, and its *previous* version, still carrying the
injected text, stayed stored and searchable (3,059 of 7,602 sessions on that vault).

When a source's record maps to an existing memory and its new body duplicates a
*different* memory, and the record is a `session` (not OpenClaw's merged session slot,
never an authored memory/skill/daily), the pipeline now drops that ref's `record_map`
row and, if no other ref still maps to the stale memory, deletes it from the index and
deletes its vault file when the file still carries that id. This matches what ADR 0012
already does for a duplicate arriving fresh: one copy is kept. `Index.delete` also
removes the memory's `body_hash_index` rows, so a deleted memory can no longer make the
pipeline drop identical content as a duplicate of nothing.
- New wrappers Codex adds later will need adding to the list. The fleet health check's
  per-vault size and the share of session text are the signal.
