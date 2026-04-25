# Memstem

Unified memory and skill infrastructure for AI agents. One canonical knowledge store. Many AI clients. No version-fragility.

> A central memory with stems reaching out to other systems, drawing their memories in.

## What it is

Memstem is a **standalone memory service** that acts as the single source of truth for memories and skills shared across multiple AI environments. Unlike traditional memory layers that you push to from each AI, Memstem **pulls** from the filesystem of each connected AI — so it's immune to upgrade churn in any of them.

Connect Claude Code, OpenClaw, Codex, Cursor, Aider, Hermes — Memstem watches each system's session and memory files, ingests new content within seconds, and exposes one unified search API via MCP.

## Why

Existing AI memory systems break when their host upgrades. Push-based hooks fail silently across version changes. Each AI has its own memory format, and there's no clean way to share knowledge across them.

Memstem solves this by:

- **Pull-based ingestion** via `inotify` filesystem watchers — no hooks, no push APIs to break
- **Markdown-canonical storage** — files are the truth, the index is rebuildable
- **Hybrid search** — BM25 (FTS5) + cosine similarity (sqlite-vec) + reciprocal rank fusion
- **Multi-AI adapters** — pluggable per-system ingestion (Claude Code, OpenClaw, Codex, etc.)
- **MCP-native API** — every modern AI agent can call it

## Architecture (one paragraph)

Markdown files in a structured tree are the canonical store. A SQLite database with FTS5 and sqlite-vec is the rebuildable index. A daemon watches each connected AI's filesystem with `inotify` plus a 5-minute reconciliation pass, ingests deltas, and updates the index. An MCP server exposes search, get, and skill retrieval to clients. A hygiene worker dedupes, decays, and auto-extracts skills from session transcripts.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full design.

## Status

🚧 **Pre-alpha.** Public API and storage layout are not yet stable. Repo is private until v0.1 ships.

## Quickstart (planned)

```bash
# Install
pip install memstem

# Initialize a vault
memstem init ~/memstem-vault

# Connect adapters
memstem adapter add claude-code --watch ~/.claude/projects
memstem adapter add openclaw --watch ~/ari/memory

# Start the daemon
memstem daemon

# Search
memstem search "what did I decide about pricing"
```

For Claude Code integration, register Memstem's MCP server in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "memstem": {
      "command": "memstem",
      "args": ["mcp"]
    }
  }
}
```

## Platform support

| OS | v0.1 support | Notes |
|---|---|---|
| Linux | ✅ Tested | Primary development platform. CI gates merges on Python 3.11 + 3.12. |
| macOS | ⚠️ Supported, not CI-gated | `watchdog` uses FSEvents and the daemon runs. The CI runner's `actions/setup-python` ships a Python without `enable_load_extension`, which `sqlite-vec` needs, so macOS jobs run as `continue-on-error: true` for visibility. A user-installed Python (e.g. `brew install python@3.11`) has extension support enabled and works. |
| Windows | ❌ Use WSL2 | Native Windows runs in CI for visibility (`continue-on-error: true`) but is not supported. Run Memstem inside WSL2 for v0.1; native PowerShell support is on the v0.2+ roadmap. |

## Documentation

- [Architecture](./ARCHITECTURE.md) — system design and rationale
- [Roadmap](./ROADMAP.md) — release plan
- [Frontmatter spec](./docs/frontmatter-spec.md) — the markdown schema
- [MCP API](./docs/mcp-api.md) — tool definitions
- [Adapters](./docs/adapters/) — per-system ingestion
- [Decisions](./docs/decisions/) — Architecture Decision Records

## License

MIT — see [LICENSE](./LICENSE).

## Acknowledgments

Memstem builds on ideas from:

- [basic-memory](https://github.com/basicmachines-co/basic-memory) — markdown + wikilinks pattern
- [doobidoo/mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) — sqlite-vec hybrid retrieval reference
- [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — index/log pattern
- [Graphiti](https://github.com/getzep/graphiti) — bi-temporal facts
- [Anthropic memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) — abstract memory interface
