# 0051 — Index Claude Code user skills

Status: **Accepted**
Date: 2026-10-10
Related: ADR 0019 (no skill authoring), ADR 0022 (Codex adapter)

## Context

MemStem indexes skills from OpenClaw workspaces (`<workspace>/skills/`) and from Codex
(`~/.codex/skills/`, ADR 0022), but not from Claude Code. Claude Code has the same
mechanism: a user skill is `~/.claude/skills/<name>/SKILL.md` (or under
`$CLAUDE_CONFIG_DIR`). No decision ever excluded it. The Claude Code adapter was built in
April 2026 for session transcripts and CLAUDE.md files, and skills were never added. The
gap stayed hidden on the maintainers' own server because every entry in its
`~/.claude/skills` is a symlink to an OpenClaw workspace skill that MemStem already indexes.

On other installs the gap is real. A client who writes skills with Claude Code, or who
gets them from a tool, cannot find them through `memstem_search` or `memstem_list_skills`.
Pocket Code 1.34 now installs its `pocket-projects` and `pocket-files` skills into both
`~/.claude/skills` and `~/.codex/skills`, and a Claude-only server indexes neither.

Two hazards shaped the design, both seen on real servers:

- **Copies.** Symlinked skills point at a canonical copy indexed elsewhere. Claude's apps
  also sync vendor skills into `~/.claude/skills/synced/<id>/<name>/SKILL.md`, often
  three copies of each (`docx`, `pdf`, `xlsx`, …). Tools such as Pocket Code install
  byte-identical copies for Claude Code and Codex.
- **Vault path collisions.** A skill's vault path comes from its title (`skills/<slug>.md`,
  or `skills/<agent>/<slug>.md` when the record carries an `agent:` tag). An untagged
  Claude Code skill would share a path with the Codex skill of the same name.

## Decision

The Claude Code adapter indexes user skills:

1. **Exactly one level:** `<skills_root>/<name>/SKILL.md`, as Claude Code loads them.
   Deeper files (including `synced/…`) are not read.
2. **Skip hidden folders** (`.trash`, `.system`, …) **and anything reached through a
   symlink** (the folder or the `SKILL.md`). The canonical copy is indexed by its own
   adapter.
3. **Skip a byte-identical copy of a Codex user skill** with the same folder name, when
   the Codex adapter indexes skills. The Codex record stands for both.
4. **Tag each record `agent:claude-code`**, so its vault path is
   `skills/claude-code/<slug>.md` and cannot collide with Codex or OpenClaw skills.
5. **Config:** `adapters.claude_code.skills_root` (default `$CLAUDE_CONFIG_DIR/skills`,
   else `~/.claude/skills`) and `adapters.claude_code.ingest_skills` (default `true`).
   The watcher covers the root, so a new or edited skill is indexed within seconds.

Parsing reuses the Codex adapter's markdown-to-record function: the same frontmatter,
title and tag handling for every client's `SKILL.md`.

## Consequences

- Claude-only installs see their own skills in search and `memstem_list_skills`.
- Installs whose `~/.claude/skills` holds only symlinks, vendor copies or Codex duplicates
  gain no records. Measured on brads-server: zero new records.
- A skill installed for both clients but edited in only one of them is indexed twice,
  once per client. That is correct: the two copies now say different things.
- A user who keeps nested folders of their own skills deeper than one level will not see
  them indexed. Claude Code does not load those either.

## Alternatives considered

- **Follow symlinks and deduplicate by content.** Rejected: more code for no gain, since
  the target is already indexed with its real provenance.
- **Leave records untagged and let the later writer win the vault path.** Rejected: two
  sources would alternate ownership of one file on every reconcile.
- **Deduplicate against every adapter, not just Codex.** Rejected for now: OpenClaw skills
  of the same name are usually different procedures for a different agent; the Codex case
  is the one tools create on purpose.
