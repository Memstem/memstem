# Memstem — Obsidian plugin

First-party Obsidian plugin for browsing, searching, and editing your Memstem vault.

## Status

**v0.1 (scaffold).** This release proves the integration end-to-end:
the plugin connects to `memstem daemon`'s local HTTP server and shows
the daemon's status in the Obsidian status bar. Search modal, sidebar
pane, and "New memory" command land in subsequent versions.

## Requirements

- Obsidian 1.4 or newer (desktop only — the plugin talks to a local
  HTTP server, which Obsidian Mobile doesn't support).
- A running `memstem daemon` (`pm2 start memstem -- daemon` or
  equivalent). The daemon's HTTP server is enabled by default and binds
  to `127.0.0.1:7821`.
- Your Obsidian vault should be opened on `~/memstem-vault` (or
  whatever `vault_path` your `_meta/config.yaml` points at). The plugin
  doesn't move files around; it operates on the vault Obsidian already
  has open.

## Installing (BRAT, recommended)

While we're pre-community-store:

1. Install the [BRAT](https://github.com/TfTHacker/obsidian42-brat)
   plugin from the Obsidian community plugin store.
2. In BRAT settings, click **Add Beta Plugin** and paste:
   `https://github.com/Memstem/memstem`
3. Enable **Memstem** in **Settings → Community plugins**.

BRAT will keep the plugin up-to-date from new GitHub releases tagged
on this repo.

## Installing (manual)

1. Download `manifest.json`, `main.js`, and (if present) `styles.css`
   from the latest [release](https://github.com/Memstem/memstem/releases).
2. Drop them into `<your-vault>/.obsidian/plugins/memstem/`.
3. Restart Obsidian; enable **Memstem** in **Settings → Community plugins**.

## Settings

- **Daemon URL** — default `http://127.0.0.1:7821`. Override if your
  daemon listens on a different port (`http.port` in
  `_meta/config.yaml`).
- **Health-check interval (ms)** — how often the status bar pings the
  daemon. `0` disables polling; the status bar then only updates on
  manual command invocation.

## Development

```bash
cd clients/obsidian
npm install
npm run build      # one-shot production build → main.js
# or
npm run dev        # watch mode
```

For local testing, symlink the build output into a test vault:

```bash
mkdir -p ~/test-vault/.obsidian/plugins/memstem
ln -sf "$(pwd)/manifest.json" ~/test-vault/.obsidian/plugins/memstem/
ln -sf "$(pwd)/main.js"       ~/test-vault/.obsidian/plugins/memstem/
ln -sf "$(pwd)/styles.css"    ~/test-vault/.obsidian/plugins/memstem/
```

Restart Obsidian after each `npm run build`.

## License

MIT (matches the parent Memstem repo).
