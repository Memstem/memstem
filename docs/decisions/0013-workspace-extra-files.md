# ADR 0013: Per-workspace top-level extra files

Date: 2026-04-29
Status: Accepted

## Context

The OpenClaw adapter auto-ingests three things from every workspace:

- `MEMORY.md` (top-level core file, configurable via `layout.memory_md`)
- `CLAUDE.md` (top-level instructions, configurable via `layout.claude_md`)
- `memory/**/*.md` and `skills/**/SKILL.md` (recursive)

Plus `OpenClawAdapterConfig.shared_files` for cross-agent files like
`HARD-RULES.md` (tagged `shared`, no `agent:*` tag).

Real workspaces grow more top-level system files than the two the
layout knows about. Ari has 23 top-level `.md` files including
`SOUL.md` (identity), `USER.md` (operator profile), `AGENTS.md` (the
agent fleet), `IDENTITY.md`, `TOOLS.md`, `RELATIONSHIPS.md`,
`PRIORITIES.md`, `COMMUNICATION.md`, `ESCALATION.md`, plus dated
snapshots and incident reports. Only `MEMORY.md`, `CLAUDE.md`, and
`HARD-RULES.md` (via `shared_files`) currently make it into the
index. Searching `memstem search "SOUL.md identity"` returns sessions
where `SOUL.md` was discussed but no direct hits on its content — a
real recall gap on the agent's own foundational files.

`shared_files` works around it but tags content as `shared`, which is
semantically wrong for agent-specific identity files and breaks any
future filter that asks "give me everything for `agent:ari`".

This is a toolkit gap, not just an Ari config gap. Every fresh
`memstem init` install for any OpenClaw agent produces the same
coverage hole.

## Decision

Add `extra_files: list[str]` to `OpenClawLayout`. Each entry is a
workspace-relative path to a top-level markdown file the adapter
should ingest as a memory record, tagged with the workspace's
`agent:<tag>` (same treatment as `MEMORY.md` and `CLAUDE.md`).

```yaml
adapters:
  openclaw:
    agent_workspaces:
      - path: /home/ubuntu/ari
        tag: ari
        layout:
          extra_files:
            - SOUL.md
            - USER.md
            - AGENTS.md
            - IDENTITY.md
            - TOOLS.md
```

The watcher recognizes paths in `extra_files` so live edits flow
through the same way `MEMORY.md` edits do.

`shared_files` stays as-is for genuinely cross-agent content like
`HARD-RULES.md`.

## Consequences

**Pros:**

- Per-agent top-level files inherit the workspace's `agent:<tag>`
  tag, so filtered searches keep working as the schema grows.
- Watcher extends naturally — no separate code path for these files.
- Layout stays the single place describing "what counts as a
  ingestable file in this workspace."
- Setup is unblocked for Ari today, and the same field will serve
  Daymond, Fleet, and any future agent without a code change.

**Cons:**

- Each operator has to enumerate the files they want indexed.
  Auto-discovery of "every top-level `.md`" was rejected — a workspace
  often has dated snapshots, incident reports, and append-only logs
  (e.g. Ari's `DREAMS.md`, 286 KB and growing) that would either churn
  the index or add noise. Explicit > implicit here.

**Mitigations:**

- `discovery.py` can grow a `discover_workspace_extras` helper later
  that proposes a curated list (skipping `*_FULL_*`, `INCIDENT-*`,
  obvious log files) for the init wizard to surface. Out of scope for
  this ADR — the field lands first, smarter discovery comes when a
  second agent needs it.
