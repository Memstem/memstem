#!/usr/bin/env bash
# Memstem one-line installer.
#
# Designed so an agent (or a human) can install memstem and its
# dependencies with a single curl-and-pipe:
#
#   curl -fsSL https://memstem.com/install.sh | bash
#
# For an unattended ("agent runs this for the user") install, pass --yes:
#
#   curl -fsSL https://memstem.com/install.sh | bash -s -- --yes
#
# A complete cutover (install + import history + start daemon + wire
# clients) is one invocation:
#
#   curl -fsSL https://memstem.com/install.sh \
#     | bash -s -- --yes --connect-clients --migrate --start-daemon
#
# Steps, all idempotent:
#   1) verify Python 3.11+ and pipx
#   2) `pipx install memstem` (falls back to git source if PyPI install fails
#      or if --from-git is set)
#   3) install Ollama and pull `nomic-embed-text` (skip with --no-ollama),
#      and confirm the daemon is reachable on :11434
#   4) `memstem init <vault>` — passes -y when --yes is set so the wizard
#      doesn't hang
#   5) (with --migrate) run `memstem migrate --apply` to import history
#   6) `memstem doctor` to confirm everything is wired up
#   7) (with --connect-clients) `memstem connect-clients` to patch
#      settings.json + CLAUDE.md (passes --remove-flipclaw if set)
#   8) (with --start-daemon) start the daemon under PM2 and pm2 save
#
# Re-running is safe: each step detects an existing install and either
# skips or upgrades.

set -euo pipefail

YES_FLAG=false
INSTALL_OLLAMA=true
PULL_MODEL=true
CONNECT_CLIENTS=false
REMOVE_FLIPCLAW=false
RUN_MIGRATE=false
MIGRATE_DAYS=30
MIGRATE_NO_EMBED=false
START_DAEMON=false
VAULT_PATH="${MEMSTEM_VAULT:-$HOME/memstem-vault}"
SOURCE="${MEMSTEM_INSTALL_SOURCE:-pypi}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES_FLAG=true; shift ;;
    --no-ollama) INSTALL_OLLAMA=false; PULL_MODEL=false; shift ;;
    --no-model) PULL_MODEL=false; shift ;;
    --vault) VAULT_PATH="$2"; shift 2 ;;
    --from-git) SOURCE=git; shift ;;
    --connect-clients) CONNECT_CLIENTS=true; shift ;;
    --remove-flipclaw) REMOVE_FLIPCLAW=true; shift ;;
    --migrate) RUN_MIGRATE=true; shift ;;
    --migrate-days) MIGRATE_DAYS="$2"; shift 2 ;;
    --migrate-no-embed) MIGRATE_NO_EMBED=true; shift ;;
    --start-daemon) START_DAEMON=true; shift ;;
    -h|--help)
      cat <<'EOF'
Memstem installer.

Usage: install.sh [options]

Options:
  --yes, -y           Run unattended (no prompts; use defaults). Propagated
                      to `memstem init -y`.
  --no-ollama         Don't install Ollama.
  --no-model          Don't pull the embedding model (assume it's already there).
  --vault PATH        Vault location (default: ~/memstem-vault).
  --from-git          Install from the GitHub source instead of PyPI.
  --connect-clients   After install, run `memstem connect-clients` to wire
                      Claude Code (settings.json + CLAUDE.md) and every
                      OpenClaw workspace's CLAUDE.md. Prints a unified
                      diff (dry-run) before applying, then applies.
  --remove-flipclaw   With --connect-clients, also strip the legacy
                      claude-code-bridge.py SessionEnd hook.
  --migrate           After init, run `memstem migrate --apply` to import
                      historical Ari/OpenClaw memory and recent Claude
                      Code sessions into the new vault.
  --migrate-days N    Claude Code session lookback for --migrate (default 30).
                      Smaller values reduce the embed load on a fresh
                      install — recent sessions land via the daemon's
                      watch loop instead.
  --migrate-no-embed  With --migrate, skip vector embedding (records still
                      land in vault + FTS5). Run `memstem reindex` later
                      to backfill embeddings overnight.
  --start-daemon      After everything else, start `memstem daemon` under
                      PM2 (`pm2 start ... --name memstem; pm2 save`). No-op
                      with a warning if pm2 isn't installed.
  -h, --help          Show this help.

Environment:
  MEMSTEM_INSTALL_SOURCE=git|pypi   Equivalent to --from-git when set to git.
  MEMSTEM_VAULT=/path/to/vault      Default vault path.
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# --- Output helpers ---------------------------------------------------------
say()  { printf '\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- Platform check ---------------------------------------------------------
case "$(uname -s)" in
  Linux*|Darwin*) ;;
  *) die "Memstem currently supports Linux and macOS only. Detected: $(uname -s)" ;;
esac

# --- Python ------------------------------------------------------------------
say "Locating Python 3.11+..."
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done
[ -z "$PY" ] && die "Python 3.11+ is required. Please install it and re-run."
ok "Python: $($PY --version)"

# --- pipx --------------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
  say "Installing pipx..."
  # Prefer a system pipx when apt is around (faster and the right place);
  # otherwise fall back to user-pip and add ~/.local/bin to PATH for this run.
  if command -v apt-get >/dev/null 2>&1 && [ -w /var/lib/dpkg/lock ] 2>/dev/null; then
    sudo apt-get install -y pipx >/dev/null 2>&1 || \
      "$PY" -m pip install --user pipx --break-system-packages 2>/dev/null || \
      "$PY" -m pip install --user pipx
  else
    "$PY" -m pip install --user pipx --break-system-packages 2>/dev/null || \
      "$PY" -m pip install --user pipx
  fi
  "$PY" -m pipx ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "pipx: $(pipx --version 2>&1 | head -1)"

# --- memstem ----------------------------------------------------------------
say "Installing memstem (source=$SOURCE)..."
case "$SOURCE" in
  pypi)
    if pipx list 2>/dev/null | grep -q '^   package memstem '; then
      pipx upgrade memstem >/dev/null 2>&1 || true
    else
      pipx install memstem >/dev/null 2>&1 || {
        warn "PyPI install failed; falling back to git source"
        SOURCE=git
      }
    fi
    ;;
esac
if [ "$SOURCE" = "git" ]; then
  pipx install --force git+https://github.com/Memstem/memstem.git
fi

if ! command -v memstem >/dev/null 2>&1; then
  die "memstem is not on PATH after install. Try: export PATH=\$HOME/.local/bin:\$PATH"
fi
ok "memstem ready"

# --- Ollama ------------------------------------------------------------------
if $INSTALL_OLLAMA; then
  if ! command -v ollama >/dev/null 2>&1; then
    say "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  ok "Ollama: $(ollama --version 2>&1 | head -1)"

  # Confirm the daemon is reachable. Linux installers set up a systemd
  # service automatically; macOS via brew does not, so try to start it.
  if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      if command -v brew >/dev/null 2>&1; then
        say "Starting Ollama via brew services..."
        brew services start ollama >/dev/null 2>&1 || true
      else
        say "Starting Ollama in background (no brew detected)..."
        nohup ollama serve >/dev/null 2>&1 &
        disown 2>/dev/null || true
      fi
    fi
    say "Waiting for Ollama on :11434..."
    for i in $(seq 1 30); do
      if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then break; fi
      sleep 1
    done
  fi
  if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    ok "Ollama daemon responding on :11434"
  else
    warn "Ollama is not responding on :11434 after 30s. The model pull will likely fail."
  fi
fi

# --- Embedding model --------------------------------------------------------
if $PULL_MODEL && command -v ollama >/dev/null 2>&1; then
  say "Ensuring nomic-embed-text is available..."
  if ollama list 2>/dev/null | grep -q '^nomic-embed-text'; then
    ok "nomic-embed-text already pulled"
  else
    ollama pull nomic-embed-text >/dev/null 2>&1 || warn "Model pull failed (continuing)"
    ok "nomic-embed-text pulled"
  fi
fi

# --- Vault -------------------------------------------------------------------
if [ -d "$VAULT_PATH/_meta" ] && [ -f "$VAULT_PATH/_meta/config.yaml" ]; then
  ok "Vault already initialized at $VAULT_PATH"
else
  say "Initializing vault at $VAULT_PATH..."
  INIT_ARGS=()
  if $YES_FLAG; then INIT_ARGS+=(-y); fi
  memstem init "${INIT_ARGS[@]}" "$VAULT_PATH"
  ok "Vault ready at $VAULT_PATH"
fi

# --- Migrate (optional) -----------------------------------------------------
if $RUN_MIGRATE; then
  say "Importing history (memstem migrate --apply, days=$MIGRATE_DAYS)..."
  MIGRATE_ARGS=(migrate --apply --vault "$VAULT_PATH" --days "$MIGRATE_DAYS")
  if $MIGRATE_NO_EMBED; then
    MIGRATE_ARGS+=(--no-embed)
  fi
  # The migrate command prints counts + sample previews as it processes;
  # let it stream to the operator's terminal.
  memstem "${MIGRATE_ARGS[@]}" || warn "migrate reported issues — review above"
  ok "history imported"
fi

# --- Doctor ------------------------------------------------------------------
say "Running memstem doctor..."
memstem doctor --vault "$VAULT_PATH" || warn "doctor reported issues — review above"

# --- Connect clients --------------------------------------------------------
if $CONNECT_CLIENTS; then
  say "Previewing connect-clients changes (dry-run)..."
  CONNECT_BASE=(connect-clients --vault "$VAULT_PATH")
  if $REMOVE_FLIPCLAW; then
    CONNECT_BASE+=(--remove-flipclaw)
  fi
  memstem "${CONNECT_BASE[@]}" --dry-run || true

  say "Applying connect-clients..."
  memstem "${CONNECT_BASE[@]}" || warn "connect-clients reported issues — review above"
fi

# --- Start daemon (optional) ------------------------------------------------
if $START_DAEMON; then
  if command -v pm2 >/dev/null 2>&1; then
    MEMSTEM_BIN="$(command -v memstem)"
    say "Starting memstem daemon under PM2 (vault=$VAULT_PATH)..."
    # pm2 won't accept duplicate names; restart in place if it already exists.
    if pm2 describe memstem >/dev/null 2>&1; then
      pm2 restart memstem >/dev/null 2>&1 || true
    else
      pm2 start "$MEMSTEM_BIN" \
        --name memstem \
        --interpreter none \
        -- daemon --vault "$VAULT_PATH"
    fi
    pm2 save >/dev/null 2>&1 || true
    ok "memstem daemon online (pm2 logs memstem)"
  else
    warn "pm2 not installed; skipping --start-daemon. Run \`memstem daemon\` manually, or install PM2: npm i -g pm2"
  fi
fi

# --- Next steps --------------------------------------------------------------
if $START_DAEMON && $CONNECT_CLIENTS; then
  cat <<EOF

\033[1mMemstem is installed, ingesting, and wired into Claude Code.\033[0m

Verify it's working:

  pm2 logs memstem --lines 20
  memstem search "your query here"

Vault:  $VAULT_PATH
Config: $VAULT_PATH/_meta/config.yaml
EOF
elif $CONNECT_CLIENTS; then
  cat <<EOF

\033[1mMemstem is installed and wired into Claude Code.\033[0m

Next steps:

  1) Start the daemon to ingest your memory:
       memstem daemon
     (or re-run this installer with --start-daemon to put it under PM2)

  2) Try a one-shot search:
       memstem search "your query here"

Vault:  $VAULT_PATH
Config: $VAULT_PATH/_meta/config.yaml
EOF
else
  cat <<EOF

\033[1mMemstem is installed.\033[0m

Next steps:

  1) Start the daemon to ingest your memory:
       memstem daemon
     (or re-run this installer with --start-daemon to put it under PM2)

  2) Wire Memstem into Claude Code (and any OpenClaw CLAUDE.md):
       memstem connect-clients
     or re-run this installer with --connect-clients to do it automatically.

  3) Try a one-shot search:
       memstem search "your query here"

Vault:  $VAULT_PATH
Config: $VAULT_PATH/_meta/config.yaml
EOF
fi
