# ADR 0010: Obsidian plugin + local HTTP API

Date: 2026-04-27
Status: Accepted

## Context

Memstem's vault is plain markdown with YAML frontmatter (ADR 0002). That
shape is, by accident or by design, exactly what Obsidian expects from a
vault — so users could already `Open folder as vault` on
`~/memstem-vault` and see every memory, daily log, skill, and session
as a navigable note. What they'd lose by doing that:

- **Hybrid retrieval.** Obsidian's built-in search is keyword-only and
  doesn't see Memstem's vector index. A query like "how do I restart
  the relay so new env settings actually take effect" finds nothing,
  even though Memstem's `memstem search` returns the right session at
  rank 1.
- **Per-result ranking metadata.** Memstem produces `bm25_rank`,
  `vec_rank`, and a fused score for every hit; Obsidian shows none of
  it.
- **Frontmatter scaffolding for new memories.** Memstem records require
  `id`/`type`/`created`/`updated`/`source` in YAML frontmatter, plus
  type-specific fields like `scope` and `verification` for skills. A
  raw `Ctrl+N` in Obsidian creates a file the daemon ignores because it
  fails frontmatter validation.

Without an integration, Memstem and Obsidian are two unconnected views
on the same files: Memstem's CLI/MCP knows the index, Obsidian shows
the markdown. The user wanted these two views unified.

## Decision

Ship a first-party Obsidian plugin in this repo (`clients/obsidian/`)
that connects to a local HTTP server co-hosted in `memstem daemon`. The
plugin is read-and-write — Obsidian becomes a full editing surface over
the vault, with Memstem-aware search and frontmatter scaffolding.

Four sub-decisions, each in its own section below.

### 1. Plugin lives in this repo, not a sister repo

Rationale:

- The plugin is a thin client of Memstem core. Their lifecycles are
  coupled: when the search API or frontmatter schema changes, both
  ship together. Atomic version bumps avoid the "which plugin version
  works with which core version" matrix.
- The MCP server already lives at `src/memstem/servers/mcp_server.py`.
  Adding `clients/obsidian/` mirrors that pattern on the user-facing
  side.
- Discovery: people landing on the Memstem GitHub repo see the plugin
  immediately. A sister repo (`memstem-obsidian`) creates a discovery
  gap.

Cost: a TypeScript/Node toolchain in a Python repo. Mitigations: the
plugin builds independently (`cd clients/obsidian && npm run build`),
and the CI workflow only runs the TS build on changes under
`clients/obsidian/**`. `clients/obsidian/node_modules/` and the built
`main.js` are gitignored; releases publish them as artifacts.

### 2. HTTP API co-hosted in `memstem daemon`

The plugin needs a way to call Memstem's hybrid search from inside
Obsidian's renderer process. Three options were considered:

| Option | Pros | Cons |
|---|---|---|
| Plugin spawns `memstem search --json` subprocess per query | No new daemon surface | Cold-starts the embedder every call; awkward from inside Electron sandbox |
| Plugin reads SQLite directly + calls Gemini from TS | No daemon at all | Reimplements the search pipeline in TS; will drift from Python source |
| **HTTP server in the daemon** (chosen) | One process, one log; reuses live `Search`/`Index`/`Embedder` instances; loopback-only so no auth needed | Adds a new dependency (FastAPI + uvicorn) |

The HTTP server runs as one more `asyncio.create_task` inside
`_run_daemon`. It binds to `127.0.0.1:7821` by default (port
configurable in `_meta/config.yaml`), exposes endpoints that mirror the
MCP tool list one-to-one, and shares the same `Vault`, `Index`, and
`Embedder` instances the watch loop and embed worker already use.
Because everything is loopback, no authentication is needed for v0.1.

A separate `memstem serve` command was rejected: it doubles process
management for no benefit. Co-hosting means installing the plugin and
restarting the daemon is the entire setup — no extra service to start
and supervise.

### 3. Distribution: BRAT first, community store later

The Obsidian community plugin store has 2–8 week review cycles and is
cumbersome for fast iteration on early API versions.
[BRAT](https://github.com/TfTHacker/obsidian42-brat) is a community
plugin that installs other plugins directly from any GitHub repo URL,
auto-updating from new tags. For early adopters and the primary user
(Brad), BRAT is the right v0.1 distribution.

The plugin is built from day one to meet community-store requirements
(no telemetry without consent, no remote code execution, MIT license,
valid manifest, desktop-only flag set correctly) so that submission
later is administrative, not a rewrite.

Realistic submission target: after ~30 days of production use and at
least one minor version bump where the API surface didn't change.

### 4. Read AND write semantics

The plugin is read/write from v0.1. Editing in Obsidian fires the
daemon's `watchdog` watcher, which re-reads the file's frontmatter +
body and re-indexes — this already works for any other source of
edits, so Obsidian is just another editor from the daemon's
perspective.

Two practical wrinkles addressed:

- **New-file creation needs frontmatter scaffolding.** Memstem records
  require specific frontmatter fields, type-dependent. A raw "Create
  new note" in Obsidian produces an invalid file. The plugin provides
  a "Memstem: New memory" command that prompts for type
  (memory/skill/daily/session), generates a UUID, fills `created` and
  `updated` with now-ISO, and writes a stub the user can flesh out.
- **Edit conflicts are rare but possible.** If the user saves in
  Obsidian during the daemon's reconcile of that file, the watcher
  emits a re-ingest — last write wins. In practice this is fine
  because both sources read the same file. If it ever causes problems
  we add a per-path advisory file lock; not for v0.1.

The `_meta/index.db` file is binary SQLite — Obsidian won't render it
but the plugin's settings and the README explicitly tell the user not
to manually edit it.

## Consequences

### Wins

- Obsidian becomes a full editing surface: graph view, backlinks,
  Dataview, the user's preferred editor — all on top of the same
  vault Memstem indexes.
- A new user can install the plugin and immediately have hybrid
  search inside their existing Obsidian workflow.
- Atomic releases: a single git tag bumps both core and plugin.
- Future clients (web, VS Code extension, etc.) can reuse the HTTP
  server — the design isn't Obsidian-specific.

### Costs

- Repo gains a TS toolchain. Contributors who only want to touch the
  Python code don't have to install Node, but reviewers of plugin PRs
  do.
- One more port to manage on the host (loopback `7821`). Conflicts
  resolved via config knob.
- FastAPI + uvicorn in the dependency list. Both are stable and
  widely deployed; the marginal install size is small.
- The plugin's read/write semantics make Obsidian a writer to the
  vault. Edit conflicts are theoretically possible. Mitigated by the
  watch-loop pattern; revisit if it bites.

### Followups

- v0.1 plugin features: search modal + sidebar pane + "New memory"
  command + status-bar daemon indicator. Each in its own follow-up
  PR.
- HTTP API additions: `POST /upsert`, `GET /skills`, websocket for
  live updates. Each as needed by plugin features.
- CI: `clients-obsidian.yml` workflow that runs `npm run build` and
  uploads release artifacts on tag.
- Community-store submission once API is stable.

## Status

Accepted; scaffold landed in PR #(this PR).
