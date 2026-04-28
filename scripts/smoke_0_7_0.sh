#!/usr/bin/env bash
# Memstem 0.7.0 production smoke test.
#
# Read-only / dry-run by default. Verifies that the six v0.7.0 stages
# (importance ranking, retrieval log, hygiene importance bumps,
# distillation candidates, dedup candidates, dedup judge) are wired up
# end-to-end against a live vault without mutating it beyond the
# unavoidable `query_log` row a single search writes.
#
# Usage:
#   VAULT=/path/to/vault bash scripts/smoke_0_7_0.sh
#   VAULT=$HOME/memstem-vault MEMSTEM_BIN=/path/to/memstem bash scripts/smoke_0_7_0.sh
#
# Exit code 0 = every step passed. Nonzero = at least one step failed
# or timed out. Failed steps are listed at the end so the operator
# does not have to scroll through the log.
#
# Knobs (env vars):
#   VAULT                  required. Vault path. No default — refuse
#                          to guess; an unset VAULT is treated as a
#                          configuration error.
#   MEMSTEM_BIN            optional. Path to the `memstem` binary.
#                          Default: whatever `memstem` resolves to on
#                          $PATH.
#   STEP_TIMEOUT           optional. Per-step timeout in seconds.
#                          Default 30. Override for slow disks /
#                          large vaults.
#   DEDUP_MAX_MEMORIES     optional. Cap the outer loop in the
#                          dedup-candidates check. Default 5 — small
#                          enough to finish in a smoke window.
#   HTTP_HOST              optional. Daemon host for /health and
#                          /search. Default 127.0.0.1.
#   HTTP_PORT              optional. Daemon port. Default 7821.
#   SMOKE_QUERY            optional. Lexical-friendly probe for the
#                          search check. Default "memstem".
#
# What it explicitly does NOT do (by design):
#   - Never runs `memstem hygiene importance --apply`. Only the
#     dry-run path is exercised, so importance values on disk do not
#     change.
#   - Never runs `memstem hygiene dedup-judge --enable-llm`. The
#     Ollama judge is gated behind that flag; this script only
#     surfaces the warning that even the default NoOpJudge writes
#     to the `dedup_audit` table, and refuses to invoke it on a
#     production vault during a smoke run.
#   - Never deletes, merges, or marks memories. Every sweep is
#     either fully read-only or dry-run.
#
# What it CAN incidentally write:
#   - One `query_log` row per HTTP /search hit and per `memstem search`
#     CLI invocation, when `hygiene.query_log_enabled = true` (the
#     default). To keep this run fully read-only, set
#     `hygiene.query_log_enabled = false` in `_meta/config.yaml`
#     before running the script.
#
# This script is meant to be safe to run against a production vault.
# When in doubt, read the section it tests in `docs/operations.md`
# first and re-confirm the read-only contract.

set -uo pipefail

MEMSTEM="${MEMSTEM_BIN:-memstem}"
STEP_TIMEOUT="${STEP_TIMEOUT:-30}"
DEDUP_MAX_MEMORIES="${DEDUP_MAX_MEMORIES:-5}"
HTTP_HOST="${HTTP_HOST:-127.0.0.1}"
HTTP_PORT="${HTTP_PORT:-7821}"
SMOKE_QUERY="${SMOKE_QUERY:-memstem}"

if [[ -z "${VAULT:-}" ]]; then
  printf 'FATAL: VAULT is unset. Pass VAULT=/path/to/vault.\n' >&2
  exit 2
fi

if [[ ! -d "$VAULT" ]]; then
  printf 'FATAL: VAULT=%s is not a directory.\n' "$VAULT" >&2
  exit 2
fi

if ! command -v "$MEMSTEM" >/dev/null 2>&1; then
  printf 'FATAL: %s not on PATH. Set MEMSTEM_BIN or `pipx install memstem`.\n' "$MEMSTEM" >&2
  exit 2
fi

if ! command -v timeout >/dev/null 2>&1; then
  printf 'FATAL: GNU coreutils `timeout` not found.\n' >&2
  exit 2
fi

PASS=0
FAIL=0
SKIP=0
FAILED_STEPS=()

step() {
  printf '\n\033[1m▶ %s\033[0m\n' "$1"
}

ok() {
  printf '  \033[32m✓\033[0m %s\n' "$1"
  PASS=$((PASS + 1))
}

bad() {
  printf '  \033[31m✗\033[0m %s\n' "$1"
  FAIL=$((FAIL + 1))
  FAILED_STEPS+=("$1")
}

skip_note() {
  printf '  \033[33m~\033[0m %s\n' "$1"
  SKIP=$((SKIP + 1))
}

run_with_timeout() {
  # run_with_timeout <label> <command...>
  local label="$1"
  shift
  local out
  if out="$(timeout --foreground --kill-after=5 "$STEP_TIMEOUT" "$@" 2>&1)"; then
    ok "$label"
    if [[ -n "$out" ]]; then
      printf '%s\n' "$out" | sed 's/^/    /'
    fi
    return 0
  else
    local rc=$?
    if [[ $rc -eq 124 || $rc -eq 137 ]]; then
      bad "$label (timed out after ${STEP_TIMEOUT}s)"
    else
      bad "$label (exit=$rc)"
    fi
    if [[ -n "$out" ]]; then
      printf '%s\n' "$out" | sed 's/^/    /'
    fi
    return 1
  fi
}

printf '\033[1mMemstem 0.7.0 smoke test\033[0m\n'
printf '  vault:        %s\n' "$VAULT"
printf '  binary:       %s (%s)\n' "$MEMSTEM" "$(command -v "$MEMSTEM")"
printf '  step timeout: %ss\n' "$STEP_TIMEOUT"
printf '  dedup cap:    --max-memories %s\n' "$DEDUP_MAX_MEMORIES"
printf '  http:         %s:%s\n' "$HTTP_HOST" "$HTTP_PORT"

# --- 1. health check ---------------------------------------------------------

step '1/6: health check (memstem doctor)'
run_with_timeout 'memstem doctor reports vault + index healthy' \
  "$MEMSTEM" doctor --vault "$VAULT" || true

# --- 2. HTTP search check ----------------------------------------------------

step "2/6: HTTP search probe (POST http://$HTTP_HOST:$HTTP_PORT/search)"
if ! command -v curl >/dev/null 2>&1; then
  skip_note 'curl not on PATH; skipping HTTP probe'
else
  HEALTH_BODY="$(timeout --foreground --kill-after=2 5 \
    curl -fsS "http://$HTTP_HOST:$HTTP_PORT/health" 2>/dev/null || true)"
  if [[ -z "$HEALTH_BODY" ]]; then
    skip_note "no daemon listening on $HTTP_HOST:$HTTP_PORT (start with: $MEMSTEM daemon --vault $VAULT)"
  else
    ok "GET /health responded: $HEALTH_BODY"
    SEARCH_BODY="$(timeout --foreground --kill-after=2 "$STEP_TIMEOUT" \
      curl -fsS -X POST "http://$HTTP_HOST:$HTTP_PORT/search" \
        -H 'Content-Type: application/json' \
        -d "{\"query\": \"$SMOKE_QUERY\", \"limit\": 3}" 2>&1 || true)"
    if [[ -n "$SEARCH_BODY" && "$SEARCH_BODY" != *error* ]]; then
      ok "POST /search returned ($(printf '%s' "$SEARCH_BODY" | wc -c) bytes)"
    else
      bad "POST /search failed: $SEARCH_BODY"
    fi
  fi
fi

# --- 3. query_log + importance dry-run --------------------------------------

step '3/6: query_log + hygiene importance dry-run'
# A single CLI search to make sure the query_log path writes a row.
# This is the only intentional write the smoke test causes against a
# vault with default config; the dry-run hygiene importance below does
# not advance the cursor or mutate frontmatter.
run_with_timeout 'memstem search (writes one query_log entry)' \
  "$MEMSTEM" search --vault "$VAULT" --limit 1 "$SMOKE_QUERY" || true

run_with_timeout 'memstem hygiene importance (dry-run; no apply)' \
  "$MEMSTEM" hygiene importance --vault "$VAULT" || true

# --- 4. distillation candidate check ----------------------------------------

step '4/6: distillation candidate report (read-only)'
run_with_timeout 'memstem hygiene distill --min-cluster-size 5' \
  "$MEMSTEM" hygiene distill --vault "$VAULT" --min-cluster-size 5 || true

# --- 5. dedup candidates (bounded preview) ----------------------------------

step "5/6: dedup-candidates bounded preview (--max-memories $DEDUP_MAX_MEMORIES)"
# A full scan is roughly O(N^2) in indexed memories — on Brad's vault
# that exceeds the default 30-second timeout easily. The bounded preview
# caps the outer loop so the sweep always finishes inside the smoke
# window. The `--limit` flag only caps the *report*; the work is bounded
# by `--max-memories`.
run_with_timeout "memstem hygiene dedup-candidates --max-memories $DEDUP_MAX_MEMORIES --neighbors 2 --limit 5" \
  "$MEMSTEM" hygiene dedup-candidates \
    --vault "$VAULT" \
    --max-memories "$DEDUP_MAX_MEMORIES" \
    --neighbors 2 \
    --limit 5 || true

# --- 6. dedup-judge warning -------------------------------------------------

step '6/6: dedup-judge (skipped on production vault)'
skip_note 'dedup-judge writes to dedup_audit even with the default NoOpJudge.'
skip_note "Run manually after reviewing docs/operations.md → '0.7.0 production smoke test'."
skip_note "  $MEMSTEM hygiene dedup-judge --vault \"$VAULT\" --max-memories $DEDUP_MAX_MEMORIES --limit 5"
skip_note '  Add --enable-llm only after explicit approval to spend Ollama cycles.'

# --- summary ----------------------------------------------------------------

printf '\n\033[1mSummary\033[0m\n'
printf '  pass:    %d\n' "$PASS"
printf '  fail:    %d\n' "$FAIL"
printf '  skipped: %d\n' "$SKIP"

if [[ $FAIL -gt 0 ]]; then
  printf '\n\033[31mFailed steps:\033[0m\n'
  for s in "${FAILED_STEPS[@]}"; do
    printf '  - %s\n' "$s"
  done
  exit 1
fi

printf '\n\033[32m0.7.0 smoke OK\033[0m\n'
exit 0
