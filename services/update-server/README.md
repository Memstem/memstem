# updates.memstem.dev

The service behind MemStem's daily update check (ADR 0050). `server.py` is stdlib-only
Python and is the complete record of what the service receives and keeps — see
[docs/privacy.md](../../docs/privacy.md).

Run: `python3 server.py` (listens on 127.0.0.1:8790; put a TLS proxy such as a Cloudflare
Tunnel in front). State lives in `~/.local/share/memstem-updates/` (`checkins.sqlite3`,
`id-hash-secret`, `stats-token`; override with `MEMSTEM_UPDATES_*` env vars).

Aggregates: `curl -H "Authorization: Bearer $(cat ~/.local/share/memstem-updates/stats-token)" https://updates.memstem.dev/v1/stats`
