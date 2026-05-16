# Codex client setup

Wires OpenAI's Codex CLI to use Memstem as its memory backend, so
every Codex session can search across every agent's memories, skills,
and prior sessions.

This is the **manual** setup. A `memstem connect-clients codex`
command that does the wiring automatically (the way the existing
`connect-clients` handles Claude Code and OpenClaw) is planned as a
follow-up.

## Prerequisites

- Codex CLI installed and on `PATH` (`codex --version` works).
- Memstem installed and on `PATH` (`memstem --version` works).
- Memstem daemon running, or at least the vault initialized (`memstem
  init`).

If Codex and Memstem are on different hosts, this setup does not
apply by itself — see the remote-MCP architecture notes in the
project README. The single-host case is what the templates here
cover.

## Step 1 — Register the Memstem MCP server

Append `config.toml.fragment` to your `~/.codex/config.toml`:

```bash
cat <PATH-TO-MEMSTEM-REPO>/clients/codex/config.toml.fragment \
  >> ~/.codex/config.toml
```

That adds:

```toml
[mcp_servers.memstem]
command = "memstem"
args = ["mcp"]
```

Codex will spawn `memstem mcp` over stdio on first use and expose its
tools (`memstem_search`, `memstem_get`, `memstem_list_skills`,
`memstem_get_skill`, `memstem_upsert`) to the model.

Verify Codex sees it:

```bash
codex mcp list
```

## Step 2 — Install the Memstem-first directive

Copy `AGENTS.md.example` to `~/.codex/AGENTS.md`:

```bash
cp <PATH-TO-MEMSTEM-REPO>/clients/codex/AGENTS.md.example \
   ~/.codex/AGENTS.md
```

If you already have a `~/.codex/AGENTS.md`, merge the relevant
sections in by hand — Codex concatenates `AGENTS.md` files from the
global level down through your project tree, so make sure the
Memstem-first rule is somewhere in the chain.

## Step 3 — Confirm ingestion

The Memstem daemon's Codex adapter watches:

- `~/.codex/sessions/**/rollout-*.jsonl` — session transcripts
- `~/.codex/skills/<name>/SKILL.md` — your user skills
- `~/.codex/memories/*.md` — free-form user memories

`~/.codex/skills/.system/` is excluded by design (vendor-shipped
skills are not personal memory; see ADR 0022).

After starting the daemon, confirm Codex sessions are appearing in
search:

```bash
# Run a quick Codex session that does something memorable, then:
memstem search "<topic from that session>"
```

You should see the new session in the results, alongside any Claude
Code or OpenClaw memories about the same topic.

## Step 4 — Use it

Start a Codex session and ask a retrieval-style question:

> What did we decide about the auth middleware rewrite?

Codex should call `memstem_search` (you'll see the tool invocation in
its commentary), pull the relevant memory, and answer with the
context that lives outside this session.

## Troubleshooting

**Codex doesn't list Memstem under `codex mcp list`.** Check the TOML
syntax is intact (`codex --help` rejects malformed config), make sure
the `memstem` binary is on `PATH` for the shell Codex inherits from,
and confirm there's no stale `[mcp_servers.memstem]` block elsewhere
in `config.toml`.

**Codex never calls `memstem_search`.** The model decides when to
call tools. Make sure `AGENTS.md` is in place and the directive is
clear; if the model has the tool but still grep-walks the repo,
sharpen the language in `AGENTS.md` (e.g., add concrete examples of
queries that should hit Memstem first).

**Sessions aren't being ingested.** Check `memstem daemon` is
running, and that its logs show `reconcile complete (codex): N
records` on startup. Common causes: vault path mismatch, `~/.codex`
on a path the daemon process can't read (different user, different
container, network mount that doesn't fire inotify events).
