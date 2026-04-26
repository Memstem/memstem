# Architecture

## Goals

1. **Single canonical store** for memories and skills shared across multiple AI clients
2. **Immunity to upgrade churn** in any individual AI client
3. **Sub-second ingestion** of new content from any source
4. **Hybrid search** — keyword + semantic — with no remote API dependencies (optional)
5. **Human-readable** canonical layer (markdown + frontmatter)
6. **MCP-native** integration

## Layered design

```
┌──────────────────────────────────────────────────────────┐
│                    AI Clients                            │
│  Claude Code  │  OpenClaw  │  Codex  │  Cursor  │  ...   │
└──────────────────────────────────────────────────────────┘
                          ▲ MCP / HTTP
                          │
┌──────────────────────────────────────────────────────────┐
│                  Memstem Daemon                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │   Servers   │  │   Hygiene    │  │   Adapters     │  │
│  │  MCP / HTTP │  │ Worker (bg)  │  │ Watchers (bg)  │  │
│  └─────────────┘  └──────────────┘  └────────────────┘  │
│         ▲                ▲                  ▲           │
│         └────────────────┼──────────────────┘           │
│                          │                              │
│  ┌─────────────────────────────────────────────────┐    │
│  │            Index (SQLite + FTS5 + vec)          │    │
│  └─────────────────────────────────────────────────┘    │
│                          ▲                              │
│  ┌─────────────────────────────────────────────────┐    │
│  │      Canonical Storage (markdown vault)         │    │
│  │  memories/  skills/  sessions/  daily/          │    │
│  └─────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
                          ▲
                          │ inotify watches
┌──────────────────────────────────────────────────────────┐
│              External AI Filesystems                     │
│  ~/.claude/projects/  ~/ari/memory/  ~/codex/sessions/   │
└──────────────────────────────────────────────────────────┘
```

## Two-layer storage

### Canonical: markdown files in a structured tree

```
~/memstem-vault/
├── memories/
│   ├── people/
│   │   └── brad-besner.md
│   ├── decisions/
│   │   └── 2026-04-25-deploy-via-cloudflare.md
│   └── facts/
│       └── port-assignments.md
├── skills/
│   ├── deploy-to-kinsta.md
│   └── send-telegram.md
├── sessions/
│   └── 2026-04-25/
│       ├── claude-code-abc123.md
│       └── openclaw-def456.md
├── daily/
│   └── 2026-04-25.md
└── _meta/
    ├── taxonomy.md
    └── config.yaml
```

Every file has YAML frontmatter (see [frontmatter-spec.md](./docs/frontmatter-spec.md)). Files survive any version of any tool, are diffable, git-friendly, and openable in Obsidian.

### Index: SQLite with FTS5 + sqlite-vec

A single `index.db` file rebuilt from the canonical store at any time. FTS5 provides BM25 keyword retrieval; sqlite-vec provides cosine similarity over embeddings. Hybrid queries use Reciprocal Rank Fusion (RRF) to merge.

If the index is ever corrupted, lost, or becomes incompatible: `memstem reindex` rebuilds it from the canonical files. The truth is never at risk.

## Pull-based ingestion

Each adapter is a small module that knows how to read one external AI's filesystem.

- **Claude Code adapter**: watches `~/.claude/projects/*/sessions/*.jsonl` with `inotify`, extracts user/assistant turns, dedupes, writes a clean memory record into `memories/sessions/`.
- **OpenClaw adapter**: watches `~/ari/memory/`, `~/ari/skills/`, daily logs. Mirrors directly into the canonical vault.
- **Codex adapter** (planned): watches `~/.codex/sessions/`.
- **Cursor adapter** (planned): watches Cursor's local memory store.
- **Generic file adapter** (planned): watches an arbitrary directory provided by the user.

Adapters emit normalized memory records. Storage and indexing are downstream — adapters never touch the index directly.

A 5-minute reconciliation pass runs alongside `inotify` to catch anything missed (crashes, race conditions, files moved out-of-band).

## Hybrid search

Every query goes through three stages:

1. **Embed the query** using the configured embedding backend. Memstem ships four pluggable implementations: `OllamaEmbedder` (default, local, `nomic-embed-text` 768d), `OpenAIEmbedder` (with `base_url` knob for OpenAI-compatible providers), `GeminiEmbedder` (`gemini-embedding-2-preview` default with Matryoshka support, so 768d Ollama indexes can switch over without reindexing), and `VoyageEmbedder` (Anthropic's recommended partner). Backend and dimensions are configured in `_meta/config.yaml`; see ADR 0009.
2. **Run two retrievals in parallel**:
   - FTS5 BM25 over the markdown body + frontmatter tags
   - sqlite-vec cosine similarity over chunk embeddings
3. **Merge with RRF** (k=60 default): combined ranking is the normalized inverse-rank sum from both retrievers.

Optional third signal (planned): entity-link retrieval over wikilinks (`[[Entity]]`) extracted at ingest time.

Optional fourth signal (planned): recency + importance score from the hygiene worker.

## Hygiene worker

Runs in a background thread, processing the canonical store at a low priority:

- **Dedup**: pairs with cosine similarity > 0.95 are merged; the higher-importance record wins, the duplicate becomes a redirect.
- **Decay**: importance score decays over time; bursts of recall raise it.
- **Skill extraction**: an LLM pass on multi-step procedures from session transcripts. Successful procedures become skills (`skills/auto/...`).
- **Bi-temporal validity** (planned): when a fact contradicts an existing one, the old one gets `valid_to: <date>` rather than being deleted.

## API surface

**MCP tools** (primary):

- `memstem_search(query, limit=10, types=[...]) -> [Result]`
- `memstem_get(id_or_path) -> Memory`
- `memstem_list_skills(scope=...) -> [Skill]`
- `memstem_get_skill(name) -> Skill`
- `memstem_upsert(content, frontmatter) -> Memory` (write path)

**HTTP API** (secondary): same shape under `/api/v1/`.

**Anthropic memory-tool adapter** (planned flagship feature): implements `BetaAbstractMemoryTool` so Claude Code's official memory tool routes natively into Memstem.

See [docs/mcp-api.md](./docs/mcp-api.md) for full tool definitions.

## Configuration

A single `~/memstem-vault/_meta/config.yaml` file controls the daemon. See [docs/configuration.md](./docs/configuration.md) for the full schema (forthcoming).

## What Memstem is not

- Not a chat memory layer (that's mem0's space)
- Not a graph database (Graphiti, Letta)
- Not a managed cloud service (yet — see [ROADMAP.md](./ROADMAP.md))
- Not a wiki engine or note-taking app (Obsidian, Logseq)

Memstem is **infrastructure for AI agents to share knowledge without coupling to each other**.
