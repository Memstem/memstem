#!/usr/bin/env bash
# End-to-end smoke test — exercises the full happy path against a throwaway vault.
#
# What this verifies (~15s, no network, no API keys):
#
#   1. `memstem init -y` creates a config + vault skeleton
#   2. Config rewrite to point at a fake $HOME so adapter paths resolve
#   3. Fixture markdown drops into the vault (canonical store) and Claude Code extras
#   4. `memstem doctor` reports vault, index, embed-queue, and Claude Code extras green
#   5. `memstem reindex --no-embed` walks the vault and populates the FTS5 index
#   6. `memstem search` returns the fixture by content (lexical path)
#   7. `memstem mcp` (stdio) handles `initialize` + `tools/list` + a `memstem_search` `tools/call`
#   8. `memstem connect-clients --dry-run` doesn't crash
#
# What it does NOT cover (intentionally):
#
#   - Live embedder calls (require an API key, slow, flaky)
#   - The continuous `daemon` watch loop (covered by tests/test_pipeline.py)
#   - Adapter reconcile (covered by tests/adapters/*)
#   - Cross-platform path quirks (Linux only — CI matrix covers macOS/Windows)
#
# Run before: tagging a release, flipping the repo public, accepting a
# meaningful PR. Pairs with `pytest` (component) — this is the integration
# layer. Exit code 0 = green; nonzero = at least one assertion failed.

set -euo pipefail

# --- setup ---------------------------------------------------------------

WORK_DIR="$(mktemp -d -t memstem-e2e-XXXXXX)"
VAULT="$WORK_DIR/vault"
HOME_FAKE="$WORK_DIR/home"
CC_PROJECTS="$HOME_FAKE/.claude/projects"
CC_EXTRAS="$HOME_FAKE/.claude/CLAUDE.md"
LOG_DIR="$WORK_DIR/logs"
mkdir -p "$LOG_DIR" "$CC_PROJECTS" "$(dirname "$CC_EXTRAS")"

# Use the binary on PATH unless the caller pinned one
MEMSTEM="${MEMSTEM_BIN:-memstem}"

PASS=0
FAIL=0
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

cleanup() {
  # Don't blow away the work dir on failure — the logs are useful
  if [[ $FAIL -eq 0 ]]; then
    rm -rf "$WORK_DIR"
  else
    printf '\n\033[33mLeaving work dir for inspection: %s\033[0m\n' "$WORK_DIR"
  fi
}
trap cleanup EXIT

printf '\033[1mMemstem e2e smoke test\033[0m\n'
printf 'Binary:   %s (%s)\n' "$MEMSTEM" "$(command -v "$MEMSTEM" || echo 'NOT FOUND')"
printf 'Workdir:  %s\n' "$WORK_DIR"
printf 'Vault:    %s\n' "$VAULT"

if ! command -v "$MEMSTEM" >/dev/null 2>&1; then
  printf '\033[31mFATAL: %s not on PATH. Install with `pip install -e .` or set MEMSTEM_BIN.\033[0m\n' "$MEMSTEM"
  exit 2
fi

# --- step 1: init --------------------------------------------------------

step "Step 1: memstem init -y"
if HOME="$HOME_FAKE" "$MEMSTEM" init -y "$VAULT" >"$LOG_DIR/init.log" 2>&1; then
  ok "init exit 0"
else
  bad "init exit nonzero (see $LOG_DIR/init.log)"
fi

if [[ -f "$VAULT/_meta/config.yaml" ]]; then
  ok "config.yaml created"
else
  bad "config.yaml missing"
fi

# --- step 2: point config at fake home so adapters resolve ---------------

step "Step 2: rewrite adapter paths to fake home"
# Wrapped in `if`/`then`/`else` so set -e doesn't kill the script when
# python fails — we want to record the failure via `bad` and continue.
if VAULT_PATH="$VAULT" CC_PROJECTS="$CC_PROJECTS" CC_EXTRAS="$CC_EXTRAS" \
   python3 - >"$LOG_DIR/config-rewrite.log" 2>&1 <<'PY'
import os, pathlib, yaml
cfg_path = pathlib.Path(os.environ["VAULT_PATH"]) / "_meta" / "config.yaml"
cfg = yaml.safe_load(cfg_path.read_text())
cfg.setdefault("adapters", {})
cfg["adapters"]["claude_code"] = {
    "project_roots": [os.environ["CC_PROJECTS"]],
    "extra_files": [os.environ["CC_EXTRAS"]],
}
cfg["adapters"].pop("openclaw", None)  # not testing OpenClaw here
cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("rewrote", cfg_path)
PY
then
  ok "config rewritten to fake home"
else
  bad "config rewrite failed (see $LOG_DIR/config-rewrite.log)"
fi

# --- step 3: drop fixtures (Claude Code extras + canonical vault memory) -

step "Step 3: write fixtures"
cat >"$CC_EXTRAS" <<'EOF'
# Test marker file

This is the Memstem e2e smoke test fixture. The unique phrase below should
be findable via `memstem search`.

UNIQUE_FIXTURE_PHRASE: zaphod-beeblebrox-quokka-trampoline
EOF
ok "Claude Code extras fixture written: $CC_EXTRAS"

# Canonical vault memory: tests storage + index + search without needing
# the daemon's reconcile loop (covered by tests/test_pipeline.py).
mkdir -p "$VAULT/memories"
FIXTURE_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat >"$VAULT/memories/e2e-fixture.md" <<EOF
---
id: $FIXTURE_UUID
type: memory
source: e2e-smoke
created: $NOW
updated: $NOW
title: Test marker memory
tags: [e2e, smoke, test]
---

# Test marker memory

UNIQUE_FIXTURE_PHRASE: zaphod-beeblebrox-quokka-trampoline

This memory exists so the e2e smoke test can verify that ingestion,
indexing, and search all work end-to-end against a fresh vault.
EOF
ok "vault fixture memory written ($FIXTURE_UUID)"

# --- step 4: doctor (after fixtures so the extras-path check passes) -----

step "Step 4: memstem doctor"
HOME="$HOME_FAKE" "$MEMSTEM" doctor --vault "$VAULT" >"$LOG_DIR/doctor.log" 2>&1 || true

# Doctor exits nonzero if any check fails. The embedder check fails when no
# API key is present in the env, which is normal in CI. Treat that single
# failure as expected; any other failure is a real problem.
if grep -q '✓ Vault:' "$LOG_DIR/doctor.log" \
   && grep -q '✓ Index opens cleanly' "$LOG_DIR/doctor.log" \
   && grep -q '✓ Embed queue' "$LOG_DIR/doctor.log"; then
  ok "doctor: vault, index, embed queue green"
else
  bad "doctor: missing core green checks (see $LOG_DIR/doctor.log)"
fi

if grep -q '✓ Claude Code extra:' "$LOG_DIR/doctor.log"; then
  ok "doctor: Claude Code extras path resolved"
else
  bad "doctor: Claude Code extras path NOT resolved (see $LOG_DIR/doctor.log)"
fi

# --- step 5: reindex (no-embed: lexical only) ----------------------------

step "Step 5: memstem reindex --no-embed"
if HOME="$HOME_FAKE" "$MEMSTEM" reindex --vault "$VAULT" --no-embed >"$LOG_DIR/reindex.log" 2>&1; then
  ok "reindex exit 0"
else
  bad "reindex exit nonzero (see $LOG_DIR/reindex.log)"
fi
if grep -qE "reindexed [1-9][0-9]* memor" "$LOG_DIR/reindex.log"; then
  ok "reindex picked up the fixture (count > 0)"
else
  bad "reindex saw 0 memories (see $LOG_DIR/reindex.log)"
fi

# --- step 6: search ------------------------------------------------------

step "Step 6: memstem search 'zaphod'"
HOME="$HOME_FAKE" "$MEMSTEM" search --vault "$VAULT" --limit 3 "zaphod beeblebrox quokka" >"$LOG_DIR/search.log" 2>&1 || true

if grep -q "zaphod-beeblebrox-quokka" "$LOG_DIR/search.log" \
   || grep -q "Test marker memory" "$LOG_DIR/search.log" \
   || grep -q "e2e-fixture" "$LOG_DIR/search.log"; then
  ok "search returned the fixture"
else
  bad "search did NOT return the fixture (see $LOG_DIR/search.log)"
fi

# --- step 7: MCP stdio handshake -----------------------------------------

step "Step 7: memstem mcp (stdio handshake + tools/list + tools/call)"
export VAULT_PATH="$VAULT" MEMSTEM_BIN_FOR_MCP="$MEMSTEM" HOME_FAKE="$HOME_FAKE"
# Wrapped in `if`/`then`/`else` so set -e doesn't kill the script on python failure.
if python3 - >"$LOG_DIR/mcp.log" 2>&1 <<'PY'
import asyncio, json, os, sys

VAULT = os.environ["VAULT_PATH"]
MEMSTEM = os.environ["MEMSTEM_BIN_FOR_MCP"]
HOME_FAKE = os.environ["HOME_FAKE"]

async def run():
    proc = await asyncio.create_subprocess_exec(
        MEMSTEM, "mcp", "--vault", VAULT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": HOME_FAKE},
    )

    async def send(req):
        line = (json.dumps(req) + "\n").encode()
        proc.stdin.write(line)
        await proc.stdin.drain()

    async def recv():
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not line:
            raise RuntimeError("MCP server closed stdout")
        return json.loads(line)

    try:
        # 1. initialize
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26",
                               "capabilities": {},
                               "clientInfo": {"name": "e2e", "version": "0"}}})
        init = await recv()
        assert "result" in init, f"initialize failed: {init}"
        print("initialize OK:", init["result"].get("serverInfo"))

        # 2. notifications/initialized (no response expected)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. tools/list
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_resp = await recv()
        names = sorted(t["name"] for t in tools_resp["result"]["tools"])
        print("tools:", names)
        for required in ("memstem_search", "memstem_get",
                         "memstem_list_skills", "memstem_get_skill",
                         "memstem_upsert"):
            assert required in names, f"missing tool: {required}"

        # 4. tools/call memstem_search
        await send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "memstem_search",
                               "arguments": {"query": "zaphod beeblebrox quokka",
                                             "limit": 3}}})
        call_resp = await recv()
        assert "result" in call_resp, f"tools/call failed: {call_resp}"
        content = call_resp["result"].get("content") or []
        assert content, f"tools/call returned empty content: {call_resp['result']}"
        body = content[0].get("text", "")
        assert "zaphod-beeblebrox-quokka" in body \
            or "Test marker memory" in body, f"search returned no fixture: {body[:300]}"
        print("memstem_search OK")

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if proc.stderr:
            err = (await proc.stderr.read()).decode(errors="replace")
            if err.strip():
                sys.stderr.write("---mcp stderr---\n" + err)

asyncio.run(run())
PY
then
  ok "MCP handshake + tools/list + memstem_search all green"
else
  bad "MCP server check failed (see $LOG_DIR/mcp.log)"
fi

# --- step 8: connect-clients (dry-run-equivalent: pointed at fake home) --

step "Step 8: memstem connect-clients --dry-run"
if HOME="$HOME_FAKE" "$MEMSTEM" connect-clients --vault "$VAULT" --dry-run \
     --settings "$HOME_FAKE/.claude.json" \
     --legacy-settings "$HOME_FAKE/.claude/settings.json" \
     --claude-md "$CC_EXTRAS" \
     >"$LOG_DIR/connect.log" 2>&1; then
  ok "connect-clients --dry-run exit 0"
else
  # Crash = traceback. Nonzero with a clean message is acceptable (e.g.
  # "no clients to wire") — but a Python traceback is a real bug.
  if grep -qE "Traceback" "$LOG_DIR/connect.log"; then
    bad "connect-clients crashed with traceback (see $LOG_DIR/connect.log)"
  else
    ok "connect-clients exited cleanly without crash"
  fi
fi

# --- summary -------------------------------------------------------------

printf '\n──────────────────────────────────────────\n'
printf '\033[1mResults: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
  printf '\nFailed:\n'
  for s in "${FAILED_STEPS[@]}"; do
    printf '  - %s\n' "$s"
  done
  exit 1
fi
printf '\033[32mE2E smoke test PASS\033[0m\n'
