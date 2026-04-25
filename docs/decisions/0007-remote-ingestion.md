# ADR 0007: Remote-machine ingestion is out of scope until Phase 3+

Date: 2026-04-25
Status: Accepted

## Context

Memstem watches local filesystem paths and ingests files as they appear or
change. That works perfectly when every AI client (Claude Code, OpenClaw,
etc.) runs on the same machine as the daemon — which is the v0.1 happy
path on Brad's EC2 box.

The question of remote ingestion came up early: if a user runs Claude Code
on a laptop separate from the Memstem server, can those sessions reach the
index? Two flavors of "remote" matter:

1. **Same user, multiple devices.** Laptop + workstation + server, all
   running the same agents. Sessions should converge into one searchable
   memory.
2. **Different users on the same Memstem instance.** Future B2B / hosted
   scenarios.

This ADR is about (1) — single-user multi-device. (2) is the Phase 5 hosted
offering and out of scope here.

## Options considered

**A. Sync the source files to the server, then watch them locally.**
The remote machine uses an existing tool (rsync, syncthing, iCloud Drive,
Dropbox, git-cron) to mirror its `~/.claude/projects/` (or per-agent
workspace) into a directory on the Memstem server. The Memstem daemon
treats it like any other local path. Zero new code in Memstem; works today.

**B. HTTP push API on the Memstem server.**
A thin remote client (or even Claude Code via an MCP tool) ships session
blobs to a `POST /api/v1/upsert` endpoint on the server. Memstem persists
them as if they were local. New surface area: an HTTP server, auth, rate
limiting, schema versioning over the wire.

**C. Multi-device sync (CRDT or git-based).**
Memstem itself becomes distributed: each device runs a daemon, vaults
sync bi-directionally with conflict resolution. Heaviest lift; most
correct long-term answer.

## Decision

- **v0.1 / v0.2:** support only option A. The daemon's path-based
  architecture means a synced directory looks identical to a native one,
  so this works without any code changes — just add the synced path to
  `_meta/config.yaml`.
- **Phase 3 (v0.3):** revisit option B as part of broadening the adapter
  ecosystem. If demand exists, add an optional HTTP push endpoint behind
  a token. Keep it strictly additive; the local file-watch model remains
  primary.
- **Phase 4 (v0.4):** option C. This is already on
  [ROADMAP.md](../../ROADMAP.md) as the multi-device sync feature.

## Rationale

1. **Simplicity wins for v0.1.** The whole architectural advantage of
   path-based ingestion is exactly what makes "sync your laptop's
   `~/.claude/projects/` to the server" work without writing a single
   line of new code.
2. **Network effects are weak in single-user scenarios.** A single user
   typically has a primary machine where most session activity happens.
   Sync-and-forget covers >90% of "I want my laptop sessions in the
   index" without us building a server.
3. **Building HTTP push too early multiplies surface area.** Auth, TLS,
   rate limits, schema migrations over the wire — none of which we need
   to ship a working v0.1.
4. **Existing sync tools are mature.** rsync over SSH, syncthing,
   git-on-cron, and OS-level sync (iCloud, Dropbox) all solve this
   problem with decades of operational experience. Memstem doesn't need
   to re-implement them.

## Consequences

**Pros:**

- v0.1 ships without any networking concerns beyond Ollama localhost.
- Users with multiple machines have a documented workaround that uses
  tools they already trust.
- Phase 3+ design space stays open: we can still add HTTP push (option
  B) or CRDT sync (option C) later without rewriting the local path.

**Cons:**

- Users with no existing sync setup hit a hurdle: they have to install
  rsync/syncthing/etc. and configure it before Memstem can see remote
  data. We can mitigate this in docs.
- Sync latency is whatever the chosen tool provides — typically seconds
  to minutes, not milliseconds. Acceptable for memory ingestion (which
  doesn't need real-time anyway).

## Recommended sync recipe (for the docs)

For a laptop running Claude Code that wants its sessions in a Memstem
vault on the server:

```bash
# On the laptop, every 5 minutes via cron:
*/5 * * * * rsync -a --delete ~/.claude/projects/ \
    user@memstem-server:~/claude-laptop/

# In the server's _meta/config.yaml:
adapters:
  claude_code:
    project_roots:
      - ~/.claude/projects        # local Claude Code (if any)
      - ~/claude-laptop           # synced from laptop
```

That's the entire integration. The daemon's watcher picks up changes
within milliseconds of the sync completing.
