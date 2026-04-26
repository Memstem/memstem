# Memstem

Unified memory and skill infrastructure for AI agents. One canonical knowledge store. Many AI clients. No version-fragility.

> A central memory with stems reaching out to other systems, drawing their memories in.

## What it is

Memstem is a **standalone memory service** that acts as the single source of truth for memories and skills shared across multiple AI environments. Unlike traditional memory layers that you push to from each AI, Memstem **pulls** from the filesystem of each connected AI — so it's immune to upgrade churn in any of them.

Connect Claude Code, OpenClaw, Codex, Cursor, Aider, Hermes — Memstem watches each system's session and memory files, ingests new content within seconds, and exposes one unified search API via MCP.

## Why

Existing AI memory systems break when their host upgrades. Push-based hooks fail silently across version changes. Each AI has its own memory format, and there's no clean way to share knowledge across them.

Memstem solves this by:

- **Pull-based ingestion** via `inotify` / FSEvents filesystem watchers — no hooks, no push APIs to break
- **Markdown-canonical storage** — files are the truth, the index is rebuildable
- **Hybrid search** — BM25 (FTS5) + cosine similarity (sqlite-vec) + reciprocal rank fusion
- **Multi-AI adapters** — pluggable per-system ingestion (Claude Code, OpenClaw, Codex, etc.)
- **MCP-native API** — every modern AI agent can call it

## Architecture (one paragraph)

Markdown files in a structured tree are the canonical store. A SQLite database with FTS5 and sqlite-vec is the rebuildable index. A daemon watches each connected AI's filesystem and ingests deltas. An MCP server exposes search, get, and skill retrieval to clients. A hygiene worker (Phase 2) dedupes, decays, and writes distillations from session transcripts.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full design and [ROADMAP.md](./ROADMAP.md) for the phase plan.

## Status

**v0.1 — feature-complete, soaking before tag.** The codebase ships with multi-agent OpenClaw + Claude Code adapters, hybrid search, an MCP server with five tools, a one-line installer, and an idempotent client-wiring command (`memstem connect-clients`). The repo is private until v0.1 is tagged and the soak window passes.

## Quickstart

The full one-liner. Installs everything (memstem, Ollama, embedding model), scaffolds the vault, imports your existing Claude Code + OpenClaw memory, wires Memstem into Claude Code, and starts the daemon under PM2:

```bash
curl -fsSL https://memstem.com/install.sh | bash -s -- \
  --yes --connect-clients --migrate --migrate-no-embed --start-daemon
```

The `--migrate-no-embed` flag is the practical default on a CPU-only Ollama box: it imports records to vault + FTS5 in minutes instead of hours. After it returns:

```bash
memstem search "what did we decide about pricing"   # FTS5 hits work immediately
pm2 logs memstem --lines 20                          # watch ingestion + embed worker
memstem doctor                                       # `Embed queue: N pending` shows backfill progress
```

Embedding is **always queued** rather than inline (see ADR 0009): the migrate finishes in seconds and the daemon's embed worker drains the queue at its own pace. On CPU-only Ollama that means semantic search becomes "good" over an hour or two; on the API providers below it's done in seconds.

Each flag is opt-in so you can dial back the scope:

| Flag | What it does |
|---|---|
| `--yes` | Unattended; passes `-y` to `memstem init` so the wizard doesn't prompt. |
| `--no-ollama` | Skip the Ollama install (already have it). |
| `--no-model` | Skip the `nomic-embed-text` pull. |
| `--vault PATH` | Vault location (default `~/memstem-vault`). |
| `--from-git` | Install from `github.com/Memstem/memstem` instead of PyPI. |
| `--connect-clients` | Run `memstem connect-clients` (settings.json + CLAUDE.md edits). Prints a dry-run diff before applying. |
| `--remove-flipclaw` | With `--connect-clients`, also strip the legacy `claude-code-bridge.py` SessionEnd hook. |
| `--migrate` | Run `memstem migrate --apply` to import historical memory. |
| `--start-daemon` | `pm2 start memstem` so ingestion survives reboots. |

Manual install if you'd rather not pipe a script:

```bash
pipx install memstem                         # or: pip install memstem
ollama pull nomic-embed-text                 # 768-dim local embedder
memstem init ~/memstem-vault                 # interactive wizard
memstem migrate --apply                      # one-shot history import
memstem connect-clients                      # patch settings + CLAUDE.md
memstem doctor                               # verify
memstem daemon                               # ingest + watch
```

`memstem init` runs an interactive setup wizard that finds OpenClaw agent workspaces (any directory under `$HOME` with an `openclaw.json`), shared rules files (`HARD-RULES.md`), and Claude Code's session root, then writes `~/memstem-vault/_meta/config.yaml`. Pass `-y` to auto-include every candidate with content.

`memstem connect-clients` is the cutover wiring step — it adds an `mcpServers.memstem` entry to `~/.claude/settings.json` and inserts a versioned `<!-- memstem:directive v1 -->` block into each CLAUDE.md so agents know to query Memstem for retrieval-style questions. Default mode writes `.bak` next to each edited file; `--dry-run` previews diffs without writing. Re-running is safe.

## Querying from an agent

Once `memstem connect-clients` has run, an MCP-aware client (Claude Code, etc.) sees five tools:

| Tool | Purpose |
|---|---|
| `memstem_search` | Hybrid (FTS5 + vector) search across the vault |
| `memstem_get` | Fetch a memory by id or vault path |
| `memstem_list_skills` | List skills, optionally filtered by scope |
| `memstem_get_skill` | Fetch a skill by title |
| `memstem_upsert` | Create or update a memory record |

See [docs/mcp-api.md](./docs/mcp-api.md) for the full schema.

## Configuration

`~/memstem-vault/_meta/config.yaml` controls embedding, search, and adapters. The wizard writes a sensible default; common edits:

### Embedding provider — pick one

Memstem ships four providers. Default is local Ollama; switch by editing the `embedding:` block (then `memstem reindex` so existing vectors get redone against the new provider).

```yaml
# Default — local, no API key
embedding:
  provider: ollama
  model: nomic-embed-text
  dimensions: 768
```

```yaml
# Google Gemini — same dim as Ollama (no reindex needed when switching), free tier
embedding:
  provider: gemini
  model: text-embedding-004
  api_key_env: GOOGLE_API_KEY
  dimensions: 768
```

```yaml
# OpenAI — or any OpenAI-compatible endpoint (Together, Mistral, Groq, vLLM, LM Studio)
embedding:
  provider: openai
  model: text-embedding-3-small
  api_key_env: OPENAI_API_KEY
  dimensions: 1536
  # base_url: https://api.together.xyz/v1   # for OpenAI-compatible providers
```

```yaml
# Voyage — Anthropic's recommended embedding partner; tops retrieval benchmarks
embedding:
  provider: voyage
  model: voyage-3
  api_key_env: VOYAGE_API_KEY
  dimensions: 1024
```

API keys are read from environment variables named in `api_key_env` — they never land in the vault. `embedding.workers` (default 2) and `embedding.batch_size` (default 8) tune the queue throughput; CPU Ollama is happiest at 1 worker, API providers tolerate 4+.

### Adapters

```yaml
embedding:
  provider: ollama
  model: nomic-embed-text
  base_url: http://localhost:11434
  dimensions: 768

adapters:
  openclaw:
    agent_workspaces:
      - { path: ~/ari, tag: ari }
      - { path: ~/blake, tag: blake }
    shared_files:
      - ~/ari/HARD-RULES.md
  claude_code:
    project_roots:
      - ~/.claude/projects
    extra_files:
      - ~/.claude/CLAUDE.md
```

Run `memstem doctor` after edits to verify every configured target exists and the embedder is reachable.

## Verifying it works

`memstem doctor` is the single source of truth for "is the install healthy?":

```text
$ memstem doctor
Memstem doctor (vault=/home/ubuntu/memstem-vault):

  ✓ Python 3.11
  ✓ memstem 0.1.0
  ✓ Vault: /home/ubuntu/memstem-vault
  ✓ Config: /home/ubuntu/memstem-vault/_meta/config.yaml
  ✓ Index opens cleanly
  ✓ Ollama at http://localhost:11434 (nomic-embed-text)  (768 dims)
  ✓ OpenClaw workspace: /home/ubuntu/ari (tag=ari)
  ✓ Claude Code root: /home/ubuntu/.claude/projects

All checks passed.
```

## Platform support

| OS | v0.1 support | Notes |
|---|---|---|
| Linux | ✅ Tested | Primary development platform. CI gates merges on Python 3.11 + 3.12. |
| macOS | ⚠️ Supported, not CI-gated | `watchdog` uses FSEvents and the daemon runs. The CI runner's `actions/setup-python` ships a Python without `enable_load_extension`, which `sqlite-vec` needs, so macOS jobs run as `continue-on-error: true` for visibility. A user-installed Python (e.g. `brew install python@3.11`) has extension support enabled and works. |
| Windows | ❌ Use WSL2 | Native Windows runs in CI for visibility (`continue-on-error: true`) but is not supported. Run Memstem inside WSL2 for v0.1; native PowerShell support is on the v0.2+ roadmap. |

## Documentation

- [Architecture](./ARCHITECTURE.md) — system design and rationale
- [Roadmap](./ROADMAP.md) — release plan (Phases 1–5)
- [Frontmatter spec](./docs/frontmatter-spec.md) — the markdown schema
- [MCP API](./docs/mcp-api.md) — tool definitions
- [Decisions](./docs/decisions/) — Architecture Decision Records
- [Plan](./PLAN.md) — current work breakdown

## License

MIT — see [LICENSE](./LICENSE).

## Acknowledgments

Memstem builds on ideas from:

- [basic-memory](https://github.com/basicmachines-co/basic-memory) — markdown + wikilinks pattern
- [doobidoo/mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) — sqlite-vec hybrid retrieval reference
- [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — index/log pattern
- [Graphiti](https://github.com/getzep/graphiti) — bi-temporal facts
- [Anthropic memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) — abstract memory interface
