# Privacy: the daily update check

MemStem keeps your memories on your machine. The only thing it sends out on its own is a
once-a-day update check.

## What is sent

By default the check goes to `https://updates.memstem.dev/v1/check` with:

| Field | Example | Why |
|---|---|---|
| MemStem version | `0.28.0` | to tell you whether a newer one exists |
| Operating system | `linux` | which platforms to support |
| Python minor version | `3.12` | which Pythons to support |
| Install type | `pypi` or `source` | packaged vs. source checkout |
| Install ID | random UUID | to count each install once per day |

The install ID is a random UUID created on first use and stored in
`~/.config/memstem/install-id`. It is not derived from your hardware, account or anything
else, and you can delete it at any time (a new one is made).

Cloudflare, which sits in front of the server, adds the two-letter country of the request.
The server keeps that country code. It does **not** store your IP address and keeps no
access log.

**Never sent:** memory or skill content, search queries, file paths, hostnames, usernames,
email addresses, API keys, or anything about what your agents do.

## What is kept

One row per install per day: the date, a keyed hash of the install ID (the ID itself is not
stored), version, OS, Python version, install type and country. The maintainers see
aggregate counts only (installs per day, versions, OS, countries). The complete server is
[`services/update-server/server.py`](../services/update-server/server.py) — that file is the
whole record of what it receives and keeps.

## Opting out

- Keep update notices but send nothing identifying (the check asks PyPI directly):
  `updates.anonymous_stats: false` in `_meta/config.yaml`, or set `DO_NOT_TRACK=1` or
  `MEMSTEM_NO_TELEMETRY=1`.
- Turn the check off completely: `updates.check: false`, or `MEMSTEM_NO_UPDATE_CHECK=1`.

MemStem never updates itself. A new version is only reported — in the daemon log,
`/health` (`update` block), `memstem doctor`, and once per version on an interactive terminal.
