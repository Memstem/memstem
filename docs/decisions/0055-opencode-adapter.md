# ADR 0055: OpenCode session ingestion and model provenance

Status: proposed. Date: 2026-10-28.

Pocket Code's OpenRouter engine runs OpenCode over ACP. Its conversation store is
SQLite, not Claude Code or Codex JSONL. Recall and capture are separate: OpenCode
connects to the existing MemStem MCP server; an opt-in adapter pulls its sessions.

Read the configured OpenCode database with mode=ro (including committed WAL
contents), never immutable=1. Poll every 30 seconds, replay on startup and periodic
reconcile, and let the existing pipeline deduplicate unchanged bodies. No durable
cursor can advance ahead of a successful write. Missing databases are harmless;
read/schema failures are logged and retried. Bound each session's bytes. Exclude
child sessions and configured probe directories. Keep user and assistant text;
drop synthetic/system context, reasoning, tool payloads and compaction summaries.

Use source=opencode and a stable UUID5 filename derived from the database path and
session id. Record the actual assistant message providerID/modelID, never the
session picker or requested user model. Preserve per-message attribution in the
body and provenance, and all provider/model tags when a session switches models.
This includes non-OpenRouter replies (such as old opencode/big-pickle turns).
The shared vault's existing indexing, retrieval and distillation paths apply.

Configuration is opt-in per vault, preventing another vault on the same host from
silently importing private conversations. MCP setup uses an absolute executable
and explicit vault; system instructions say to search MemStem first and retrieve
skills through its list/get tools. This does not require a new Pocket transport.
