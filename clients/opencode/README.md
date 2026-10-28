# OpenCode and Pocket Code's OpenRouter sessions

Recall and capture need separate configuration. Pocket runs `opencode acp`; the
agent loads OpenCode's native MCP and instruction settings on both new and resumed
sessions. No OpenRouter key belongs in MemStem's client configuration.

## Recall: MCP and instructions

Merge `opencode.example.json` into `~/.config/opencode/opencode.json` (or the
existing `.jsonc` file). Preserve existing providers, permissions, MCP servers and
instruction entries. Replace all example paths. Use the absolute path returned by
`command -v memstem`: service launchers may have a different PATH from your shell.
Set the vault explicitly, especially on hosts with several vaults. The example
uses `--read-only`, which serves search/get/list-skills/get-skill without upsert;
omit that flag only when deliberate memory writes are wanted too.

Copy `MEMSTEM.md` somewhere permanent and add its absolute path to `instructions`.
This supplements existing global/project instructions, instead of replacing
`AGENTS.md` (which could suppress OpenCode's fallback to `~/.claude/CLAUDE.md`).
OpenCode also discovers local Claude-compatible skills; MemStem's skill tools
retrieve the shared indexed skills, including those from other connected agents.

Check `opencode mcp list`. Start a fresh OpenCode process, or reopen the Pocket
conversation after its agent process is closed, and ask it to search a known
memory and retrieve a named skill. Verify the actual MCP tool results, not just
the model's claim that it searched. Existing agent processes retain their loaded
configuration until reopened; a Pocket restart also closes those processes.

References: [OpenCode MCP](https://opencode.ai/docs/mcp-servers/) and
[OpenCode rules](https://opencode.ai/docs/rules/).

## Capture: enable the adapter in the intended MemStem vault

This requires a MemStem build containing ADR 0055. In `_meta/config.yaml`:

```yaml
adapters:
  opencode:
    enabled: true
    database: /absolute/path/to/.local/share/opencode/opencode.db
    poll_seconds: 30
    excluded_directories:
      - /absolute/path/to/pocket-code/data/acp-probe
```

Merge into the existing `adapters` block and restart the MemStem daemon. Omit
`database` to use `$XDG_DATA_HOME/opencode/opencode.db` (default
`~/.local/share/opencode/opencode.db`). Add any other test/probe directories to
`excluded_directories`. Capture is off by default so a second vault on the same
machine cannot silently ingest the user's conversations.

The startup scan backfills existing sessions. Every poll replays records through
the usual idempotent pipeline, so a failed write is retried without losing a cursor.
The importer opens SQLite read-only, including committed WAL data. Missing files
are tolerated; schema/read failures log errors and show unhealthy while watching.
The source database remains OpenCode's responsibility.

Records use `source: opencode`, `agent:opencode`, `provider:<provider>`, and
`model:<provider>/<model>` tags. Each assistant turn names its actual model, and
`provenance.opencode.messages` retains message ids with provider/model attribution.
A conversation that switches models has all of them; old replies made by
`opencode/big-pickle` remain tagged that way. User-selected models never substitute
for recorded assistant metadata. Unknown metadata is marked unknown.

User and completed assistant text are captured. Child sessions, synthetic/ignored
text, compaction summaries, reasoning and raw tool payloads are omitted. Sessions
above `max_session_bytes` (default 16 MiB) log a warning and retain their previous
capture. Normal noise filtering, embedding and session distillation still apply.

To verify, make a substantive test conversation, wait one polling interval, and
find its wording with MemStem search. Read the result and inspect its source,
model tags and provenance. Continue the same session on another model and confirm
it updates the same record. Disable capture with `enabled: false` and restart;
existing memories stay searchable. Disable the OpenCode MCP entry separately to
remove recall access.
