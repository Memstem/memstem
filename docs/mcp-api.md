# MCP API

Memstem exposes the following tools to MCP clients.

## `memstem_search`

Hybrid keyword + semantic search across all memories and skills.

**Arguments:**

- `query` (string, required): natural language query
- `limit` (int, optional, default 10): max results
- `types` (array, optional): filter by memory type (`memory`, `skill`, `session`, etc.)
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
