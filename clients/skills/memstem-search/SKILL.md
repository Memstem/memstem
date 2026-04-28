---
name: memstem-search
slug: memstem-search
version: 0.1.0
homepage: https://github.com/Memstem/memstem
description: "Search MemStem — the unified memory index across all AI agents (Ari, Claude Code, future agents). Use for any retrieval-style question: past decisions, project status, skills lookup, prior work, 'what did we decide about X', 'do we have a skill for Y', 'what did the other agent do yesterday'. This skill owns the full priority ladder (MCP → HTTP → CLI → grep) so callers do not need to remember the order."
metadata:
  clawdbot:
    emoji: "🔎"
    requires:
      bins: ["memstem"]
    os: ["linux", "darwin"]
  openclaw:
    emoji: "🔎"
    requires:
      bins: ["memstem"]
---

# MemStem Search

Search MemStem — the unified memory index that contains every AI agent's memories, skills, daily logs, and session captures (Ari + Claude Code today, more later). MemStem is the only place that sees cross-agent context; grep on a single workspace cannot.

## When to use

Any retrieval-style question — past decisions, project status, skills lookup, prior work, fuzzy or conceptual recall. Examples:

- "what did we decide about X"
- "what's the status of Y"
- "do we have a skill for Z"
- "what did Claude Code work on yesterday"
- "find the decision about the embedder migration"

Do **not** grep on a single workspace before trying this. grep can't see other agents' memories or Claude Code session captures, and most of what you're looking for is already indexed in MemStem.

## Procedure

This skill owns the full priority ladder. Try each rung in order; only fall through on hard failure.

### 1. MemStem MCP (preferred — shares process state)

If `mcp__memstem__memstem_search` is callable in the current session, use it:

```
mcp__memstem__memstem_search(query="<the args>", top_k=10)
```

**Deferred-tool gotcha — the rule that gets missed.** Claude Code does not pre-load MCP tool schemas. If a `<system-reminder>` lists `mcp__memstem__*` as **deferred tools**, the names are advertised but the schemas are not loaded — calling them directly fails with `InputValidationError`. Load the schemas first:

```
ToolSearch(query="select:mcp__memstem__memstem_search,mcp__memstem__memstem_get,mcp__memstem__memstem_list_skills,mcp__memstem__memstem_get_skill")
```

If a later system-reminder says the memstem MCP has **disconnected**, do not retry ToolSearch — fall through to step 2.

### 2. MemStem HTTP API (warm — same daemon, loopback only)

Check the daemon:

```bash
curl -s http://127.0.0.1:7821/health
```

If `status: ok`, search:

```bash
curl -s -X POST http://127.0.0.1:7821/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "<the args>", "top_k": 10}'
```

This reuses the daemon's live `Vault` / `Index` / `Embedder` instances — same backend as the MCP, just over loopback HTTP. No subprocess cold-start.

### 3. MemStem CLI (cold-start, slower)

```bash
memstem search "<the args>" --top 10 --format json
```

**Known issues — if either of these fires, report the error in one line and proceed to step 4. Do not silently swallow:**

- `IntegrityError: UNIQUE constraint failed: embed_state.memory_id` — the index DB has duplicate rows that the connect-time backfill tries to re-insert.
- `embedder unavailable: GEMINI_API_KEY` — the CLI runs in a shell with no `GEMINI_API_KEY` exported. The MCP and the daemon have it baked into their env via the OS-level config; the CLI does not.

### 4. Fallback — grep + direct file reads

Only when steps 1–3 are all unavailable.

```bash
grep -rli "<keyword>" ~/ari/memory/ ~/ari/MEMORY.md ~/ari/skills/*/SKILL.md
```

Or read known files directly: `~/ari/MEMORY.md`, today's daily log `~/ari/memory/YYYY-MM-DD.md`, a specific `SKILL.md`.

This rung sees only the local Ari workspace — not Claude Code session captures, not other agents' memories, not the semantic index. Note this gap when you report results so the caller knows the answer may be incomplete.

## Result format

Return a concise summary, not a dump. For each top hit:

- one-line title or excerpt
- file path or memory id (so the caller can navigate)
- relevance signal (rank, score) when available

Do not paste full memory bodies unless the caller asks — assume they will follow the path.

## Reporting failures

If the entire ladder fails (rare — would mean MemStem is fully down and the workspace has nothing relevant on disk), say so explicitly. Do not pretend a no-result fallback is the same as a successful empty search.

## Do NOT

- Default to `openclaw memory search`, `memory_search`, `memory_get` — legacy paths, lower-quality recall, blind to Claude Code captures.
- Use Claude Code's built-in `~/.claude/projects/*/memory/` — disabled in this environment; MemStem is the single source of truth.
- Skip rungs because you "feel like" the answer is somewhere specific. The point of MemStem is that the index decides; the caller does not need to know where the fact lives.
