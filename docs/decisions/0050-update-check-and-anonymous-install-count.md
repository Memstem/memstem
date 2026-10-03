# 0050 — Daily update check with an anonymous install count

Status: **Accepted**
Date: 2026-10-03
Related: none (first outbound call MemStem makes on its own)

## Context

MemStem mostly runs as a background daemon used by AI agents; its owners rarely see a
terminal, so new releases (fixes such as ADR 0049) go unnoticed. The maintainers also have no
idea how many installs exist beyond their own: PyPI downloads are dominated by mirrors and
bots, and GitHub clones by CI.

## Decision

1. **Daily update check, notify only.** The daemon checks once a day (first check ~2 min after
   start) and reports a newer release in the log, `/health` (`update` block — informational,
   never degrades status), `memstem doctor` (fresh check), and once per version on an
   interactive terminal (stderr, cache only, never on pipes or MCP stdio). MemStem never
   updates itself — an unattended upgrade could run a schema migration at a bad moment.
2. **Anonymous count, on by default, disclosed, easy to refuse.** The check goes to
   `updates.memstem.dev`, which returns the latest PyPI release and records one row per install
   per day: version, OS, Python minor, install type, a keyed hash of a random install ID, and
   the country Cloudflare reports. No IP address, access log, hostname, path, username or
   content. The model follows Homebrew, Next.js, Astro and VS Code: on by default with notice.
   The disclosure is logged once on first daemon start and shown by `memstem doctor`;
   `docs/privacy.md` is the plain-language version.
3. **Opt-outs.** `updates.anonymous_stats: false`, `DO_NOT_TRACK=1` or `MEMSTEM_NO_TELEMETRY=1`
   → the check asks PyPI directly and sends nothing identifying. `updates.check: false` or
   `MEMSTEM_NO_UPDATE_CHECK=1` → no check at all.
4. **Fails safe.** If the endpoint is unreachable the check falls back to PyPI; if both fail it
   does nothing. 5 s timeout, off the daemon's critical path.
5. **Server in the repo.** `services/update-server/` (stdlib only) is the full record of what is
   received and kept.

## Consequences

- Counting covers 0.28.0 and later only; older installs stay invisible until they upgrade.
- The maintainers' own fleet sets `updates.anonymous_stats: false` so the count reflects other
  users.
- If the service is ever retired, clients fall back to PyPI automatically.
