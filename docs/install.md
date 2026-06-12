# Install guide

The [README Quickstart](../README.md#quickstart) covers the common paths. This
page is the full reference: every installer flag, API-key handling, macOS
notes, and exactly what `memstem connect-clients` edits.

## The installer

```bash
curl -fsSL https://raw.githubusercontent.com/Memstem/memstem/main/scripts/install.sh | bash -s -- \
  --yes --connect-clients --migrate --migrate-no-embed --start-daemon
```

Each flag is opt-in so you can dial back the scope:

| Flag | What it does |
|---|---|
| `--yes` | Unattended; passes `-y` to `memstem init` so the wizard doesn't prompt. |
| `--no-ollama` | Skip the Ollama install (already have it). Implied by `--embedder openai|gemini|voyage`. |
| `--no-model` | Skip the `nomic-embed-text` pull. |
| `--vault PATH` | Vault location (default `~/memstem-vault`). |
| `--from-git` | Install from `github.com/Memstem/memstem` instead of [PyPI](https://pypi.org/project/memstem/). |
| `--embedder NAME` | Embedder provider: `ollama` (default), `openai`, `gemini`, `voyage`. |
| `--openai-key KEY` | Store an OpenAI key via `memstem auth set openai`. Also reads `MEMSTEM_OPENAI_KEY`, then `OPENAI_API_KEY`. |
| `--gemini-key KEY` | Same, for Gemini (env: `MEMSTEM_GEMINI_KEY`). |
| `--voyage-key KEY` | Same, for Voyage (env: `MEMSTEM_VOYAGE_KEY`). |
| `--connect-clients` | Run `memstem connect-clients` — wires Claude Code (`~/.claude.json` + CLAUDE.md), OpenClaw, and Codex (`~/.codex/config.toml` + AGENTS.md), plus legacy-settings cleanup. Prints a dry-run diff before applying. |
| `--remove-flipclaw` | With `--connect-clients`, also strip the legacy `claude-code-bridge.py` SessionEnd hook. |
| `--migrate` | Run `memstem migrate --apply` to import historical memory. |
| `--start-daemon` | `pm2 start memstem` so ingestion survives reboots. |

### API keys

Picking `--embedder openai|gemini|voyage` implies `--no-ollama` (cloud doesn't
need a local daemon). The key gets stored via `memstem auth set <provider>`,
so cron, PM2, and fresh shells all pick it up afterward without per-shell
exports. Keys can also come from `MEMSTEM_OPENAI_KEY` / `MEMSTEM_GEMINI_KEY` /
`MEMSTEM_VOYAGE_KEY` env vars, falling back to the standard `OPENAI_API_KEY` /
`GEMINI_API_KEY` / `VOYAGE_API_KEY` names when the `MEMSTEM_*` variable is
unset (helpful for unattended installs that don't want the key on the command
line).

## The setup wizard

`memstem init` runs an interactive setup wizard that finds OpenClaw agent
workspaces (any directory under `$HOME` with an `openclaw.json`), shared rules
files, and Claude Code's session root, then writes
`~/memstem-vault/_meta/config.yaml`. Pass `-y` to auto-include every candidate
with content.

## macOS

**Use Homebrew or pyenv Python — not the system Python.** Memstem needs
`sqlite-vec`, which loads as a SQLite extension at runtime. macOS's system
Python (`/usr/bin/python3`) ships with a SQLite that has extension loading
**disabled at compile time**, so it can't load `sqlite-vec`. The `install.sh`
script detects this up front and bails with a clear error rather than letting
it crash later.

The fix is one of:

```bash
# Recommended — Homebrew
brew install python@3.12
hash -r   # let your shell pick up the Homebrew python3
curl -fsSL https://raw.githubusercontent.com/Memstem/memstem/main/scripts/install.sh | bash    # re-run
```

```bash
# Or — pyenv
pyenv install 3.12.5
pyenv global 3.12.5
curl -fsSL https://raw.githubusercontent.com/Memstem/memstem/main/scripts/install.sh | bash    # re-run
```

Both build SQLite with extension support enabled. Once you're on a Homebrew or
pyenv Python, every other step (Quickstart, manual install, `memstem doctor`)
works the same as on Linux.

Note: macOS CI is currently `continue-on-error: true` — the GitHub Actions
`setup-python` build hits the same system-Python issue. We track full macOS CI
green as a follow-up; the user-facing install path on a real Mac is reliable
today via Homebrew or pyenv.

## What `connect-clients` edits

`memstem connect-clients` is the cutover wiring step. It (a) adds an
`mcpServers.memstem` entry to `~/.claude.json` so Claude Code sees Memstem
MCP, (b) registers `mcp.servers.memstem` in each configured OpenClaw agent's
`openclaw.json` so OpenClaw agents see it too, (c) wires Codex CLI via an
idempotent `[mcp_servers.memstem]` table in `~/.codex/config.toml` plus the
same directive block in `~/.codex/AGENTS.md` (skipped when `~/.codex/` is
absent; toggle with `--codex` / `--no-codex`), (d) strips any stale entry from
the legacy `~/.claude/settings.json`, and (e) inserts a versioned
`<!-- memstem:directive v1 -->` block into each CLAUDE.md so agents know to
query Memstem for retrieval-style questions. Default mode writes `.bak` next
to each edited file; `--dry-run` previews diffs without writing. Re-running is
safe.
