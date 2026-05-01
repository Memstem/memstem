# MCP API

Memstem exposes the following tools to MCP clients.

## `memstem_search`

Hybrid keyword + semantic search across all memories and skills.

**Arguments:**

- `query` (string, required): natural language query
- `limit` (int, optional, default 10): max results
- `types` (array, optional): filter by memory type (`memory`, `skill`, `session`, `daily`, `decision`, `project`, `distillation`, etc.). Useful: `types=[distillation, project]` returns only the derived rollup records — typically what an agent wants for "what is the X project" / "what did we decide about Y" queries; `types=[memory, skill, decision]` returns only primary sources.
- `tags` (array, optional): filter by tag

**Returns:**

```json
[
  {
    "id": "<uuid>",
    "title": "<string>",
    "type": "<memory_type>",
    "snippet": "<truncated body>",
    "score": 0.85,
    "path": "<vault-relative path>",
    "frontmatter": { }
  }
]
```

## `memstem_get`

Retrieve the full content of a single memory by id or path.

**Arguments:**

- `id_or_path` (string, required): memory id or vault-relative path

**Returns:**

```json
{
  "id": "<uuid>",
  "frontmatter": { },
  "body": "<markdown content>",
  "path": "<vault-relative path>"
}
```

## `memstem_list_skills`

List available skills, optionally scoped.

**Arguments:**

- `scope` (string, optional): `universal` or specific agent name

**Returns:**

```json
[
  {
    "id": "<uuid>",
    "name": "<string>",
    "title": "<string>",
    "scope": "<string>",
    "prerequisites": []
  }
]
```

## `memstem_get_skill`

Retrieve a single skill by name.

**Arguments:**

- `name` (string, required): skill name (matches `title` slugified)

**Returns:** Same shape as `memstem_get` but for a skill record.

## `memstem_upsert` (write)

Add or update a memory. Used by adapters and by clients writing knowledge.

**Arguments:**

- `frontmatter` (object, required): must conform to [frontmatter spec](./frontmatter-spec.md)
- `body` (string, required): markdown body
- `path` (string, optional): explicit vault-relative path; auto-generated if omitted

**Returns:**

```json
{
  "id": "<uuid>",
  "path": "<vault-relative path>",
  "created": true
}
```

## Derived records (`type: distillation`, `type: project`)

Returned by the same `memstem_search` and `memstem_get` tools. They look like any other memory but carry `links` pointing back to their source records (the session a distillation summarized; the sessions + distillations a project rollup aggregated). Importance is seeded at 0.8 (distillation) / 0.85 (project) so they outrank raw transcripts on close ties.

Produced by the hygiene-worker writers, not by the adapter pipeline:

- `memstem hygiene distill-sessions [--apply] [--backfill]` — one distillation per meaningful session ([ADR 0020](./decisions/0020-session-distillation-writer.md)).
- `memstem hygiene project-records [--apply]` — one project record per Claude Code project tag with ≥2 sessions ([ADR 0021](./decisions/0021-project-records.md)).

See [docs/distillation-verification.md](./distillation-verification.md) for the operator playbook.

## Anthropic memory tool adapter (Phase 2)

When enabled, Memstem implements `BetaAbstractMemoryTool` — Claude Code's official `memory_*_20250818` tool maps directly into the Memstem vault. Tool calls (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) are translated into vault operations.

See [docs/decisions/0006-anthropic-memory-tool-adapter.md](./decisions/0006-anthropic-memory-tool-adapter.md) for the design.

## Error handling

All errors return MCP-standard error responses with one of these codes:

- `INVALID_QUERY` — query is empty or malformed
- `NOT_FOUND` — id or path doesn't exist
- `INVALID_FRONTMATTER` — frontmatter validation failed
- `INDEX_NOT_READY` — daemon is still initializing
- `INTERNAL_ERROR` — unexpected; check logs
