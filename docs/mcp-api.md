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
    "frontmatter": { },
    "embedder_degraded": false
  }
]
```

`embedder_degraded` (ADR 0032) is `true` when the embedder was unreachable
for this call (auth failure, connection error, timeout) and the results are
keyword-only — the BM25 fallback with reduced semantic recall. Agents seeing
`true` should treat thin results with suspicion and may suggest running
`memstem doctor embedder`.

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

## Read-only mode

`memstem mcp --read-only` (or `mcp.read_only: true` in `_meta/config.yaml`; either enables it) serves the four read tools — `memstem_search`, `memstem_get`, `memstem_list_skills`, `memstem_get_skill` — and does not register `memstem_upsert` at all, so it is absent from `tools/list` rather than refused at call time. The use case is a client that must search a vault it does not own, e.g. an SSH forced command in `authorized_keys`:

```
command="memstem mcp --read-only",no-pty,no-port-forwarding ssh-ed25519 AAAA… colleague
```

Prefer the flag over the config key for that: the key sits in the vault's own `config.yaml` (and would also make the owner's local `memstem mcp` read-only).

In this mode nothing a tool does writes to the vault or the index:

- The index is opened `mode=ro` with `PRAGMA query_only`. No migration, vec-table creation, journal-mode change or embedder-signature re-stamp runs. A WAL reader never takes the write lock, so it neither blocks nor is blocked by a running daemon. Because it cannot migrate, it refuses an index that does not exist yet or is below this build's schema (`ReadOnlyIndexUnavailableError`) and one whose vector dimensions differ from the config; start the daemon on that vault once, then retry. The `-wal`/`-shm` files must be readable and creatable by the user running the command (they are, for the daemon's own user).
- The query log is off (`memstem_search` result exposures and `memstem_get` opens are not recorded). That log feeds the hygiene importance bump, which rewrites memory frontmatter, so a read-only client must not feed it.
- The Jev shadow reranker (ledger write plus an external API call) is not built, and the rerank cache is not written (reranking itself, if configured, still runs).
- `memstem_get` refuses paths into underscore-prefixed vault directories (`_meta/`, `_review/`), the same rule `memstem_upsert` applies; ids and ordinary vault paths work as usual.

Query embedding still calls the configured embedder, so semantic search needs that embedder's API key available to the process, as it does normally.

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
