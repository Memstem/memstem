# ADR 0021: Project records

Date: 2026-05-01
Status: Accepted

## Context

The recall failure that motivated ADR 0020 has a second dimension:
not only does an individual session about Woodfield Country Club's
e-bike system rank weakly, but the *project as a whole* — work that
spans multiple sessions over weeks, with multiple deliverables — has
no record at the right grain.

A query like *"the project where we revised the aerial-style demo
video"* should land on a single record that says "this is the
Woodfield project; it covers e-bike + golf cart tracking; here are
the sessions, the summaries, the videos we delivered." Today the
information is scattered across:

- 4+ raw Claude Code session JSONLs, ranked weakly because
  transcripts are noisy
- Daily logs that mention Woodfield in passing
- Google Drive uploads (deliverables) that aren't in the vault at all

Once ADR 0020 lands the session distillations, the per-session signal
sharpens — but a project still has no first-class record. This ADR
adds one.

## Decision

Add **project records**: a `type: project` memory representing the
durable identity of an ongoing piece of work, accumulating links to
its sessions and distillations as the project evolves.

Project records are produced by the same summarizer abstraction as
session distillations (ADR 0020), invoked by a separate CLI
(`memstem hygiene project-records`). They land in the canonical
markdown vault with a stable path so re-runs update in place rather
than producing duplicates.

### What is a "project" — v1 definition

A v1 project is **a Claude Code project directory tag with at least
two session records**, where:

- The tag is the encoded directory under `~/.claude/projects/`,
  already extracted by `ClaudeCodeAdapter` and stored in each
  session's `tags` (e.g. `home-ubuntu-woodfield-quotes` for sessions
  in `~/.claude/projects/-home-ubuntu-woodfield-quotes/`).
- "At least two sessions" filters out one-shot / exploratory CWDs
  whose distillation is already enough.

Why Claude Code project tags as the seed signal:

- They're free — already in the vault, no new extraction needed.
- They map cleanly to the user's mental model: Brad opens
  `~/woodfield-quotes/`, every session inside that directory is "the
  Woodfield project."
- They're conservative: a session in a different CWD won't merge into
  the wrong project even if the topics overlap.

What v1 does *not* try to do:

- Merge multiple project tags that are conceptually one project (e.g.
  `home-ubuntu-woodfield-quotes` + `home-ubuntu-video-projects` for
  related Woodfield video work). The user can do this manually by
  editing the project record's `tags` field; future ADRs may add
  semi-automatic merging.
- Identify projects across OpenClaw memories. OpenClaw stores memory
  per-agent rather than per-project; there's no equivalent free
  signal. Future ADRs may add LLM-based or tag-based grouping for
  OpenClaw if the recall data warrants it.
- Treat a single-session tag as a project. Single-session work is
  already covered by the session distillation; a project record on
  top would be empty calories.

### Output shape

Project records land at `vault/memories/projects/<slug>.md`,
where `<slug>` is the lowercased Claude Code project tag (e.g.
`home-ubuntu-woodfield-quotes`).

The frontmatter:

```yaml
---
id: <uuid>
type: project
title: "Woodfield Country Club — e-bike & golf cart tracking system"
created: 2026-04-21T00:00:00Z   # earliest source session
updated: 2026-05-01T03:00:00Z   # most recent source session or rerun
source: hygiene-worker
provenance:
  source: hygiene-worker
  ref: "project:home-ubuntu-woodfield-quotes"
  ingested_at: 2026-05-01T03:00:00Z
links:
  - "memory://sessions/b7972233-e434-42fb-b55a-1736bd17e211"
  - "memory://sessions/abc1234-e434-42fb-b55a-…"
  - "memory://distillations/claude-code/b7972233-…"
  - "memory://distillations/claude-code/abc1234-…"
tags:
  - "home-ubuntu-woodfield-quotes"
  - "project:claude-code"
importance: 0.85
---
```

The body is structured by the prompt template
(`prompts/distill_project.txt`):

1. One-paragraph project description (canonical name + what the work
   is about + current status).
2. Key entities / people / organizations.
3. Major deliverables (with provenance hints when the LLM can identify
   them — e.g. "see distillation X for the aerial demo video work").
4. Accumulated decisions / constraints.
5. Active status / latest known state.

Each section is plain markdown so the body itself is searchable —
that's the whole point.

### Trigger + idempotence

- CLI command `memstem hygiene project-records [--backfill]
  [--apply] [--force]`.
- `--backfill` scans every session in the vault; default mode scans
  sessions that meet a recency window (default 30 days, configurable).
- For each Claude Code project tag with ≥ 2 sessions:
  - If no project record exists → create one.
  - If a project record exists and the set of source sessions has
    grown → update `links`, regenerate body, bump `updated`.
  - If a project record exists and source-session set is unchanged
    *and* none of the sessions' bodies have changed (compared via
    body hash) → no-op.
  - `--force` regenerates regardless.
- Re-running with `--apply` is a no-op when the input hasn't changed.

The body-hash check uses the same `body_hash` column the embed
pipeline writes. We don't recompute hashes — just read them.

### Manual override

Project records can be hand-edited like any other markdown file. To
prevent the LLM from clobbering manual content:

- A frontmatter flag `manual: true` on a project record causes the
  writer to skip body regeneration on that project (links are still
  updated when new sessions appear; body is preserved). This is the
  same pattern Memstem uses for `pinned: true` on importance.
- `--force` overrides `manual: true` (with a confirm prompt unless
  `--yes`).

### LLM choice

Same summarizer abstraction as ADR 0020. Defaults: `gpt-5.4-mini`
(OpenAI), `qwen2.5:7b` (Ollama), NoOp at install time. Cost is even
lower than session distillation because each project's body is
regenerated only when the underlying source set changes.

The prompt template is *separate* from the session distillation
template:

- `prompts/distill_session.txt` — input is one session, output is a
  one-paragraph rollup of that session.
- `prompts/distill_project.txt` — input is the project tag plus the
  bodies of all linked session distillations (or session bodies, when
  a session has no distillation), output is a structured project
  record.

The project prompt operates on session distillations preferentially
(they're already shape-optimized) and falls back to session bodies
when no distillation exists. This makes the project record a
second-order summary, which is fine for retrieval but means project
records benefit from running session distillation first.

### Search ranking

Project records seed at `importance: 0.85` — slightly above
distillations (0.8), to reflect that they're the highest-value record
for a project-shaped query. The existing alpha=0.2 multiplier
handles the search-time effect; no new search code.

A direct match on a project record (title contains "Woodfield") will
clearly outrank any specific session about Woodfield. A vec-only hit
where multiple records cluster around the project topic still has the
project record floating above the raw sessions.

## Schema additions

Like ADR 0020: zero new frontmatter fields. The `type: project` enum
value already exists in `core/frontmatter.MemoryType.PROJECT`; the
`links` / `importance` / `provenance` / `tags` fields cover everything
needed.

The `manual: true` override uses the existing pydantic `extra="allow"`
configuration on `Frontmatter` — unknown fields round-trip through
parse/serialize cleanly. A future ADR may promote it to a typed
field if it spreads beyond projects.

Internal SQLite migration:

- `v12`: `project_record_state(slug PRIMARY KEY, project_id,
  source_sessions_hash, body_hash, summarizer, ts)` — captures the
  project's input fingerprint so re-runs can short-circuit. Non-
  canonical (drop-and-rebuild safe).

## Implementation phasing

This ADR is one slice (the writer); it lands as the PR after ADR
0020's writer:

1. Schema migration v12 (`project_record_state`).
2. `hygiene/project_records.py` adds:
   - `find_project_candidates(vault, recency_days=30) -> list[ProjectCandidate]`
   - `materialize_project_record(slug, candidate, summarizer)
     -> ProjectRecord`
   - `apply_project_records(vault, plan) -> ApplyResult`
3. CLI command `memstem hygiene project-records`.
4. Tests under `tests/test_hygiene_project_records.py`.

The summarizer abstraction is shared with ADR 0020 — implementation
order is `summarizer.py` first, then session writer, then project
writer, in three sequential PRs.

## Rationale

- **Project = Claude Code project tag (v1).** This is the cheapest,
  highest-precision signal we have. Brad's mental model of "a
  project" already maps to a working directory; we re-use it. We
  don't try to be clever about cross-tag merging; that's a follow-up
  if the data shows it's needed.
- **Threshold of ≥ 2 sessions.** Below that, the session
  distillation already covers the recall need; a project record
  with one source is just noise.
- **Stable slug as the path key.** Project record paths are
  predictable (`memories/projects/<slug>.md`) so re-runs always hit
  the same file. No "did the LLM happen to pick the same canonical
  name as last time" footgun.
- **`manual: true` to protect hand-edits.** Brad will inevitably
  curate some project records (correcting names, adding context the
  LLM missed). `manual: true` says "trust me, don't regenerate this
  body." Same pattern as `pinned: true` for importance.
- **Project records summarize summaries when possible.** A project
  body produced from session distillations is more accurate than one
  produced from raw transcripts because the per-session noise has
  already been filtered. Falling back to raw bodies when no
  distillation exists keeps the v1 working without making session
  distillation a hard dependency.
- **No OpenClaw projects in v1.** OpenClaw memory layout doesn't
  carry an equivalent project signal. Adding one would be its own
  ADR worth of design — better to ship the high-confidence path now
  and revisit if recall failures specifically blame OpenClaw memory.

## Consequences

**Pros:**

- "What did we work on for X" gets a single, retrieval-shaped record
  that aggregates the session-level work — directly addressing the
  Woodfield recall failure.
- Project records improve continuity across sessions: an agent
  starting a fresh session about Woodfield can `memstem_search
  "Woodfield project"` and get the rollup, links to past
  distillations, accumulated decisions, all in one hit.
- Body is regenerable: if the LLM gets a project's framing wrong,
  re-running with `--force` (or `manual: true` for permanent
  override) is a one-line fix.
- Schema-light: zero new frontmatter fields, one new enum value
  reused (already in the codebase).

**Cons:**

- v1 only handles Claude Code projects. Brad's OpenClaw work doesn't
  benefit from this until a follow-up ADR addresses the OpenClaw
  signal.
- LLM hallucination risk on canonical names. Mitigation: provenance
  fields always tie back to source sessions; the user can edit and
  set `manual: true` if the LLM gets it wrong.
- Project tags don't always reflect projects. A user with one giant
  `home-ubuntu` directory holding many unrelated session JSONLs would
  end up with one giant "project" record. Mitigation: that's already
  a poorly-organized vault; the recall plan addresses it via
  per-project subdirectories, not via the project record writer.

## Open questions

- *Default recency window.* 30 days is a reasonable starting point;
  longer windows produce more candidates but slower runs. Tunable via
  `hygiene.project_records.recency_days`.
- *Whether session distillations should be a hard prerequisite.*
  Current design: project writer falls back to raw session bodies
  when no distillation exists. This works but produces lower-quality
  project records. v1 doesn't enforce; documentation suggests
  running session distillation first for best results.
- *Project tag normalization.* The encoded directory tag preserves
  characters like `-home-ubuntu-…`. Should the slug strip the leading
  `home-ubuntu-`? v1: no — keep the tag as-is so search by raw tag
  works. The LLM-extracted `title` provides the human-readable name.

## References

- ADR 0008 — tiered memory; this is a sibling of session distillation,
  not a tier of it.
- ADR 0019 — skill authoring out of scope; project records are
  derived summaries with mandatory provenance, the same boundary
  pattern ADR 0020 establishes.
- ADR 0020 — session distillation writer; ships before this and
  produces the inputs project records prefer to summarize.
- `src/memstem/adapters/claude_code.py` — where the project-tag
  signal originates (each session record's `tags` already includes
  the encoded directory).
- RECALL-PLAN.md — the W9 work item this ADR formalizes.
