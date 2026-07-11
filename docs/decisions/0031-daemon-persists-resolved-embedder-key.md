# 0031 — Daemon persists its env-resolved embedder key into secrets.yaml

Status: **Accepted**
Date: 2026-07-11
Supersedes: none
Related: 0030 (embed-client resilience), 0032 (surface embedder degradation)

## Context

Memstem resolves the embedder API key in two places (`auth.get_secret`): the
environment variable first, then `~/.config/memstem/secrets.yaml`. The two
copies can diverge, and when they do the failure is asymmetric and invisible:

- The **daemon** is a long-running process launched with the key in its
  environment (PM2 ecosystem file, systemd unit, container env). It always
  uses the env copy, so it embeds fine.
- **Cold-spawned processes** — the per-session `memstem mcp` stdio server that
  Claude Code / Codex spawn, and the plain `memstem` CLI — typically run with
  *no* key in their environment and fall back to `secrets.yaml`.

In the 2026-07 incident, `secrets.yaml` fleet-wide still held a pre-migration
`sk-proj` OpenAI key while the daemons carried the current self-hosted-vLLM
key in their env. Every cold-spawned MCP search got HTTP 401 from the
embedder, and the search path silently fell back to BM25 keyword-only (the
`vec query failed; falling back to BM25` warning goes to a log nobody reads at
MCP-spawn time). Semantic recall was degraded for weeks: the daemon health
check stayed green because it only exercises the daemon's own (env-keyed)
path.

The divergence class exists because a human has to remember to run
`memstem auth set` on every host whenever the key rotates — and never does.

## Decision

**On daemon startup, mirror the env-resolved embedder key into
`secrets.yaml`.** After loading config, if the configured provider's key is
present in the daemon's environment (the config's `api_key_env`, or the
provider's default env var) and the secrets file either lacks the provider
entry or holds a different value, the daemon writes the env value via
`auth.set_secret` (`auth.sync_env_secret_to_file`).

Properties:

- **Idempotent.** No write when the stored value already equals the env value,
  so a healthy restart touches nothing.
- **Guarded.** Only providers in `auth.PROVIDERS` (`openai`, `gemini`,
  `voyage`) are synced. `ollama`/local providers need no key and never get an
  entry.
- **Observable.** One info log line on write, with the key masked via
  `auth.mask` — the raw key never lands in logs.
- **Non-fatal.** A write failure (read-only `~/.config`, permissions) is
  logged as a warning; the daemon still starts.

The write goes through the existing `auth._save` path: `0o600` permissions,
atomic tmp-file replace, `MEMSTEM_SECRETS_FILE` override honored.

## Alternatives considered

- **Fix the data once** (`memstem auth set` fleet-wide): repairs today's
  divergence but not the class — the next key rotation recreates it.
- **Propagate the key into MCP registrations** (`mcp_env_from_embedding`
  already inlines the key into client config at `connect-clients` time): only
  covers clients re-registered after the rotation, and not the plain CLI.
- **Warn instead of write**: keeps the divergence and just adds noise; the
  daemon is the one process that *knows* the correct current key, so having
  it heal the fallback file is strictly better.

## Consequences

- The cold-spawn fallback file can no longer go stale relative to what the
  daemon actually uses — automatically, for every current and future tenant,
  on every daemon (re)start after a key rotation.
- New startup side-effect: the daemon writes a secret to disk. This is the
  same file, location, permissions, and content class that `memstem auth set`
  already writes; the daemon's env key was already on disk in whatever
  launcher config injects it. Threat model unchanged.
- If two daemons on one host run different keys for the same provider (not a
  supported topology today — secrets.yaml is per-user, not per-vault), the
  last one started wins the file. Acceptable: the file is a fallback for
  processes that have no better information.
- Detection of a *wrong* key on the cold path is handled separately:
  `memstem doctor embedder` (cold-path self-test) and ADR 0032 (degradation
  flag in search results).
