# ADR 0005: Pull-based ingestion via inotify, not push hooks

Date: 2026-04-25
Status: Accepted

## Context

The current "FlipClaw" pipeline uses a `SessionEnd` hook in Claude Code that runs `claude-code-bridge.py` to push session data into Ari's memory store. This pattern has broken across multiple Claude Code updates because:

- Hook config schema changes silently
- Hook timeout behavior changes silently
- Hook execution context (env vars, working dir) changes silently

Other AI memory systems (mem0, Letta, Zep) all use push-based APIs. Same fragility — coupled to client SDK versions.

## Decision

Memstem ingests **by watching filesystems**. Each adapter uses `inotify` (Linux), `fsevents` (macOS), or `ReadDirectoryChangesW` (Windows) plus a 5-minute reconciliation pass over the watched paths. No hooks. No push APIs.

## Rationale

1. **Decouples Memstem from each AI's client SDK.** AI updates can change anything internal, but as long as it still writes session/memory files to disk, Memstem ingests them.
2. **Catches sessions that crashed without firing hooks.** A session that dies mid-stream still leaves a partial JSONL on disk; Memstem reads it.
3. **Sub-second latency.** inotify fires in milliseconds; the 5-minute reconciliation is the safety net.
4. **No installation surface inside the AI client.** Nothing to register, no manifest entries.

## Consequences

**Pros:** robustness, latency, simplicity, no inter-version coupling.

**Cons:**

- Must understand each AI's on-disk format (compensated: write one adapter per AI, contributors can add more)
- Filesystem-watching has platform quirks (NFS, container mounts) that need handling

**Mitigation:** adapter base class encapsulates platform handling; per-platform fallbacks already in `watchdog`.
