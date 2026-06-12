# ADR 0010 — Obsidian plugin scaffold (withdrawn)

- Status: **Withdrawn** (number reserved)
- Date accepted: 2026-05 (PR #37)
- Date withdrawn: 2026-05 (PR #57)

## Summary

ADR 0010 originally covered the local HTTP API plus an Obsidian plugin
scaffold, introduced together in PR #37. The plugin scaffold was removed in
PR #57 before any release shipped it — releasing an empty scaffold would have
promised a feature that didn't exist yet.

The **local HTTP API half survived** and shipped: the daemon co-hosts a
`127.0.0.1:7821` HTTP server alongside MCP, and the CLI delegates to it for
sub-second queries (see [ADR 0014](./0014-cli-daemon-delegation-and-migration-discipline.md)).

If/when an Obsidian (or other editor) integration returns, it gets a fresh
ADR rather than reviving this one. The original text is in git history:
`git show ba4baf3:docs/decisions/0010-obsidian-plugin.md`.
