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
# Steps, all idempotent:
#   1) verify Python 3.11+ and pipx
#   2) `pipx install memstem` (falls back to git source if PyPI install fails
#      or if --from-git is set)
#   3) install Ollama and pull `nomic-embed-text` (skip with --no-ollama)
#   4) `memstem init <vault>` (skipped if vault already exists)
#   5) `memstem doctor` to confirm everything is wired up
#
# Re-running is safe: each step detects an existing install and either
# skips or upgrades.

set -euo pipefail

YES_FLAG=false
INSTALL_OLLAMA=true
PULL_MODEL=true
CONNECT_CLIENTS=false
REMOVE_FLIPCLAW=false
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
    -h|--help)
      cat <<'EOF'
Memstem installer.

Usage: install.sh [options]

Options:
  --yes, -y           Run unattended (no prompts; use defaults).
  --no-ollama         Don't install Ollama.
  --no-model          Don't pull the embedding model (assume it's already there).
  --vault PATH        Vault location (default: ~/memstem-vault).
  --from-git          Install from the GitHub source instead of PyPI.
  --connect-clients   After install, run `memstem connect-clients` to wire
                      Claude Code (settings.json + CLAUDE.md) and every
                      OpenClaw workspace's CLAUDE.md.
  --remove-flipclaw   With --connect-clients, also strip the legacy
                      claude-code-bridge.py SessionEnd hook.
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
  memstem init "$VAULT_PATH"
  ok "Vault ready at $VAULT_PATH"
fi

# --- Doctor ------------------------------------------------------------------
say "Running memstem doctor..."
memstem doctor --vault "$VAULT_PATH" || warn "doctor reported issues — review above"

# --- Connect clients --------------------------------------------------------
if $CONNECT_CLIENTS; then
  say "Running memstem connect-clients..."
  CONNECT_ARGS=(connect-clients --vault "$VAULT_PATH")
  if $REMOVE_FLIPCLAW; then
    CONNECT_ARGS+=(--remove-flipclaw)
  fi
  memstem "${CONNECT_ARGS[@]}" || warn "connect-clients reported issues — review above"
fi

# --- Next steps --------------------------------------------------------------
if $CONNECT_CLIENTS; then
  cat <<EOF

\033[1mMemstem is installed and wired into Claude Code.\033[0m

Next steps:

  1) Start the daemon to ingest your memory:
       memstem daemon

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

  2) Wire Memstem into Claude Code (and any OpenClaw CLAUDE.md):
       memstem connect-clients
     or re-run this installer with --connect-clients to do it automatically.

  3) Try a one-shot search:
       memstem search "your query here"

Vault:  $VAULT_PATH
Config: $VAULT_PATH/_meta/config.yaml
EOF
fi
