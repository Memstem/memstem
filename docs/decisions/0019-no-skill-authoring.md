# ADR 0019: MemStem does not author skills

Date: 2026-04-30
Status: Accepted

## Context

MemStem ingests, indexes, and surfaces three kinds of artifacts each
AI drops on disk: memories, sessions, and skills. The reads-from-disk
contract is deliberate. Per the project README and CLAUDE.md:

> Architectural advantage: immune to upgrade churn in any client
> because we depend only on the files each AI drops on disk —
> no hooks, no push APIs, no internal SDKs.

That property is what makes MemStem stable under client churn. Claude
Code can change its skill format, OpenClaw can ship a new release,
Codex can change conventions, and MemStem keeps working as long as
the resulting `SKILL.md` files (or whatever shape replaces them)
land in a path an adapter watches.

**Skill authoring** — observing a session, deciding it represents a
reusable procedure, and writing a `SKILL.md` from it — is a different
problem. Other systems Brad has used in the past have included it:

- **FlipClaw** (`~/ari/toolkit/flipclaw/scripts/skill-extractor.py`,
  ~1,440 lines) — a session-end pipeline that gates on local
  heuristics, classifies via a nano LLM, generates the SKILL.md via
  a mini LLM, dedups against existing skills, and supports an UPDATE
  mode that revises an existing skill based on a new session.
- **Hermes** (an OpenClaw competitor) — has its own skill generator.
- **Claude Code** — has a `skill-creator` skill.
- **OpenClaw** — has its own conventions and may evolve them.

Earlier MemStem planning documents (`PLAN.md`, `ARCHITECTURE.md`,
ADR 0008 PR-G, RECALL-PLAN.md W7) all mention "auto-skill
extraction" or "reflective synthesis" as planned MemStem features —
work the hygiene worker would do during a periodic LLM pass.

This ADR reverses those plans.

## Decision

**MemStem does not author skills, reflections, or any other derived
knowledge artifacts. It ingests, indexes, and serves them.**

The hygiene worker is allowed to mutate ingested records (dedup,
decay, importance bumps from query traffic, retro cleanup) — those
are bookkeeping operations on existing content, not authorship.
Anything that creates new content from an LLM pass is out of scope.

The boundary, stated as a rule:

- **In scope** for MemStem: any operation whose output is an
  annotation, deprecation marker, or index update on an existing
  record (dedup verdicts, `valid_to` expiry, `deprecated_by`
  pointers, `importance` adjustments, query log).
- **Out of scope** for MemStem: any operation whose output is a new
  vault record produced by an LLM. Skills, reflections,
  distillations-as-canonical-records, atomic-fact extractions if
  they create new records — none of those land in MemStem itself.

(ADR 0011's atomic-fact extraction is the borderline case; it
splits a session into facts. The split is one-shot at ingest, not
an ongoing authorship loop. It stays in scope under that
interpretation; future ADRs that propose ongoing LLM-authoring loops
should re-litigate against this rule.)

## Why

### 1. Format coupling

MemStem authoring skills means MemStem picking a skill format. That
format would be MemStem-shaped. Claude Code, Codex, Hermes, and
OpenClaw all have (or will have) different shapes. So either:

- The consuming AIs learn MemStem's format (the coupling we
  explicitly designed against), or
- The MemStem-authored skills are unread by the AIs that would
  actually use them.

Neither path preserves the "files-on-disk, no coupling" property.

### 2. Runtime LLM dependency

Authoring requires an LLM. Today MemStem's LLM dependencies are all
opt-in (rerank, HyDE, dedup judge — all default-off, all callable
with a NoOp fallback). The hygiene worker is otherwise pure-Python
and runs without network access. Adding skill authoring would make
a model dependency part of the daily loop.

The eval session on 2026-04-30 demonstrated empirically that
LLM-driven retrieval features can regress quality even when the
literature says they should help. Adding LLM authoring to the daily
loop multiplies the failure surface for marginal benefit when each
client AI already does this for itself.

### 3. Per-AI skill convention burden

Every AI evolves its skill conventions independently. MemStem
authoring skills means MemStem's authoring code has to track every
AI's evolving expectations. That's exactly the per-AI breaking-point
maintenance MemStem's architecture was designed to avoid.

### 4. Each AI already does this well in its own context

Claude Code's `skill-creator` knows Claude Code's session shape and
emits Claude-Code-shaped SKILL.md. Hermes does the same in its
context. FlipClaw still works for OpenClaw sessions if Brad keeps
running it. The capability exists where it belongs — close to the
session source, in the format the producing AI uses.

MemStem's job is to make all of those skills, regardless of who
authored them, searchable in one place. That's the value-add. Adding
a competing authorship pipeline doesn't compound the value; it
duplicates work in the wrong layer.

## What changes

### Removed from the roadmap

- `PLAN.md` line 386 (the "Auto-skill extraction" bullet) — gone.
- `ARCHITECTURE.md` line 116 (hygiene worker's "Skill extraction"
  bullet) — gone, with a forward pointer to this ADR.
- ADR 0008 PR-G ("auto-skill extraction" implementation phase) —
  removed; ADR 0008 retains its first six PRs.
- ADR 0008's "Auto-skill extraction" capability bullet — removed,
  with a forward pointer to this ADR.
- RECALL-PLAN.md W7 ("Reflective synthesis") — removed. The
  reflective-synthesis design proposed weekly LLM passes that wrote
  `type: reflection` records to the vault. Same authorship pattern,
  same problem — out of scope by this ADR.

### Stays unchanged

- Adapter ingestion of `SKILL.md` files (current OpenClaw shape,
  whatever Claude Code uses, future formats). Read-only flexibility
  on the adapter side is welcome and necessary.
- The `skill-review` queue in `hygiene/cleanup_retro.py` — that's a
  hygiene operation on existing skills (collision routing), not
  authorship.
- The `type: skill` enum value, the importance-type weight for
  skills (0.7), and the higher search ranking they get.
- ADR 0011's atomic-fact extraction (one-shot per session at
  ingest, see boundary discussion above).

### Not affected

- W5 (cross-encoder rerank, ADR 0017): retrieval-time, no authoring.
- W6 (HyDE query expansion, ADR 0018): query-time, no authoring.
- ADR 0012's dedup judge: produces verdicts on existing records,
  doesn't author new ones.

## Consequences

**Good:**

- The "files-on-disk" architectural property stays intact. MemStem
  remains immune to per-AI skill-format churn.
- The hygiene worker stays pure-Python and network-free in its
  default loop. Opt-in LLM features remain opt-in.
- The decision is explicit and load-bearing: future sessions don't
  re-propose this and end up rebuilding what we just decided not to
  build.
- The clients that *should* author skills (Claude Code, Hermes,
  OpenClaw, Codex, future AIs) keep doing so in their native
  format, which is where that knowledge naturally belongs.

**Acceptable:**

- MemStem will never produce reflective summaries, cross-cluster
  pattern records, or auto-extracted skills. If those become
  desirable, they land in the AI that owns the source (FlipClaw
  rebuilt for a new agent, a Claude Code skill that runs at
  session-end, etc.) — not in MemStem.

**Closed off:**

- The "MemStem as second-brain author" framing some prior planning
  documents leaned into. MemStem is a search-and-storage layer over
  what other AIs author. Not a synthesizer.

## Rationale (one-line)

MemStem's value is being the unified read layer across every AI's
artifacts. Authoring a competing artifact stream undermines that
mission and reintroduces the exact coupling the
read-files-from-disk design eliminates.

## References

- `CLAUDE.md` — "immune to upgrade churn... no hooks, no push APIs"
- `ARCHITECTURE.md` §"Hygiene worker"
- ADR 0008 ([0008-tiered-memory.md](./0008-tiered-memory.md)) —
  removes PR-G from its phasing.
- ADR 0011 ([0011-noise-filter-and-fact-extraction.md](./0011-noise-filter-and-fact-extraction.md))
  — atomic-fact extraction's one-shot scope is preserved per the
  boundary discussion above.
- RECALL-PLAN.md — W7 removed.
- FlipClaw `skill-extractor.py` — reference design Brad has used in
  the past for skill authoring at the AI layer.
