# Memstem Operations

Operator-facing reference for running Memstem in production. Targeted
at the human + AI agent doing release smokes, hygiene sweeps, and
incident response. Component-level docs (`mcp-api.md`,
`frontmatter-spec.md`, the ADRs) describe *what* the system does;
this doc tells you *how to run it safely*.

## 0.7.0 production smoke test

Run before promoting 0.7.0 to a live vault, and after every upgrade
that touches the hygiene worker. The full procedure is wired up in
`scripts/smoke_0_7_0.sh`; this section explains what each step
asserts, what it can and cannot mutate, and the warnings the operator
needs to see before running.

### Read-only contract

The smoke test is **safe to run against a production vault** when:

- `hygiene.query_log_enabled` is acceptable (see "What it can write"
  below) — the script writes a small number of `query_log` rows. If
  even that is unacceptable, set the flag to `false` in
  `_meta/config.yaml` for the duration of the run.
- The dedup-judge step is **skipped** (the default; see step 6).

The smoke test is **not** safe to run against a production vault if
you change any of these defaults without reading the rest of this doc:

- `--apply` on `hygiene importance` — applies bumps and advances the
  cursor.
- `--enable-llm` on `dedup-judge` — invokes a live Ollama call per
  candidate pair and writes its verdict to `dedup_audit`.
- Removing `--max-memories` on `dedup-candidates` — full-vault scans
  on a >1k-memory vault routinely exceed a 30-second smoke timeout
  (see step 5).

### Running it

```bash
VAULT=$HOME/memstem-vault bash scripts/smoke_0_7_0.sh
```

Knobs (all env vars, all optional):

| Var | Default | Purpose |
|---|---|---|
| `VAULT` | *required* | Vault path. No default — refuse to guess. |
| `MEMSTEM_BIN` | `memstem` (PATH) | Override the binary location. |
| `STEP_TIMEOUT` | `30` | Per-step timeout in seconds. |
| `DEDUP_MAX_MEMORIES` | `5` | Cap on the dedup-candidates outer loop. |
| `HTTP_HOST` | `127.0.0.1` | Daemon host for `/health` + `/search`. |
| `HTTP_PORT` | `7821` | Daemon port. |
| `SMOKE_QUERY` | `memstem` | Lexical-friendly probe. |

Exit code `0` = every step passed (or the daemon was not running for
step 2 — that's a skip, not a failure). Nonzero = at least one step
failed or timed out; the script lists the failed steps at the end.

### What each step asserts

#### 1. Health check

`memstem doctor --vault "$VAULT"` reports vault + index + embedder
green. Asserts the basic post-upgrade invariants: schema migrations
applied, `memories_vec` reachable, embedder configured.

Read-only.

#### 2. HTTP search probe

`GET http://$HTTP_HOST:$HTTP_PORT/health` followed by
`POST /search` with a small lexical query. Asserts the live daemon
is answering — both the health endpoint added in 0.6.0 and the
search endpoint that the `memstem-search` skill calls.

If the daemon is not running, the step is **skipped** (not failed);
the smoke test never tries to start a daemon. If you need it, run
`memstem daemon --vault "$VAULT"` in another shell first.

Writes one `query_log` row when `hygiene.query_log_enabled = true`
(the default). Set the flag to `false` in `_meta/config.yaml` to
keep it fully read-only.

#### 3. query_log + hygiene importance dry-run

`memstem search` writes a `query_log` entry; `memstem hygiene
importance --dry-run` reads the log and prints proposed bumps.

The dry-run path is the **default**; the smoke script never passes
`--apply`. Without `--apply`, the cursor in `hygiene_state` does
not advance, no frontmatter is written, and re-running the dry-run
keeps showing the same proposals. To actually apply, the operator
runs `memstem hygiene importance --apply` separately, after a human
review of the proposed changes.

Writes one or two `query_log` rows (depending on hits returned).
No frontmatter changes.

#### 4. Distillation candidate report

`memstem hygiene distill --min-cluster-size 5` lists clusters of
memories that *could* be summarized into a single distillation
record (topic-tag clusters or same-agent ISO-week daily-log
clusters). **Read-only.** No LLM calls, no vault writes; just a
report.

The actual LLM-driven distiller that turns a cluster into a
`type=distillation` memory is a future PR behind an explicit config
flag. As of 0.7.0, this command exists only to surface candidates
the operator can review.

#### 5. Dedup candidates (bounded preview)

`memstem hygiene dedup-candidates --max-memories N --neighbors 2
--limit 5` reports memory pairs whose first-chunk embeddings are
above the cosine threshold (default 0.85). **Read-only.** No
auto-merge.

**Why `--max-memories`.** The function issues one `query_vec` per
indexed memory. Each `query_vec` is a vec0 k-NN MATCH that scans
the full `memories_vec` table, so the total work is roughly O(N²)
in vault size — several tens of seconds on a ~1k-memory vault and
minutes on a 5k-memory vault. The `--limit` flag only caps the
*report*, not the *work*: even with `--limit 1`, the loop still
walks every memory.

`--max-memories N` caps the *outer loop* at the first N memory ids
(sorted by id), so the sweep finishes in O(M·N) and is bounded by
the smoke timeout. Production full scans should be run async, not
inside a smoke window:

```bash
nohup memstem hygiene dedup-candidates --vault "$VAULT" \
  > /var/log/memstem/dedup-candidates.log 2>&1 &
```

The `--max-memories` value picked by the smoke script (default `5`)
is *not* a recommended steady-state value — it's the smallest cap
that still exercises the code path. For an actual triage sweep, use
a much higher cap or omit the flag entirely.

#### 6. Dedup-judge warning

The smoke script **does not invoke `dedup-judge`** on a production
vault. It prints the manual command and the warnings instead.

> ⚠️ **`dedup-judge` writes to `dedup_audit` even with the default
> `NoOpJudge`.** The audit table receives one row per candidate pair
> on every run — that is the design (the table is the inventory the
> future resolution PR consumes). The verdict differs by judge:
> `NoOpJudge` records `UNRELATED`; `OllamaDedupJudge` records the
> model's actual verdict. **Neither writes to vault frontmatter as of
> 0.7.0.** A future PR will read `applied = 0` rows and apply safe
> verdicts to `deprecated_by` / `valid_to` / `supersedes` / `links`.

To run dedup-judge manually after explicit approval:

```bash
# NoOp judge — no LLM, but rows still land in dedup_audit
memstem hygiene dedup-judge \
  --vault "$VAULT" \
  --max-memories 5 \
  --limit 5

# Real judge — invokes Ollama per pair; spends LLM cycles
memstem hygiene dedup-judge \
  --vault "$VAULT" \
  --max-memories 5 \
  --limit 5 \
  --enable-llm \
  --ollama-url http://localhost:11434 \
  --ollama-model qwen2.5:7b
```

Run with the smallest `--max-memories` that exercises the path you
want to evaluate; scale up only after auditing the resulting rows
in `dedup_audit`. The audit table is queryable directly:

```bash
sqlite3 "$VAULT/_meta/index.db" \
  "SELECT verdict, COUNT(*) FROM dedup_audit WHERE applied = 0 GROUP BY verdict;"
```

### What the smoke script can write

In its default configuration, the smoke script can cause:

- **`query_log` rows** from the single CLI search and the HTTP search
  (one row per hit). These are bounded by
  `hygiene.query_log_max_rows` (default 100k) and FIFO-pruned
  automatically.

It cannot, by default, cause:

- Frontmatter changes (no `--apply`, no resolution PR exists yet).
- `dedup_audit` rows (step 6 is skipped on production).
- Ollama API calls (no `--enable-llm` anywhere).
- New vault files (no upserts).

### When something fails

Failed steps are listed at the end of the run. Common modes:

- **Step 1 fails: `embedder unavailable`.** The persistent secret
  store is missing the API key. `memstem auth set <provider>
  <KEY>` and re-run.
- **Step 2 skipped: no daemon.** Start `memstem daemon --vault
  "$VAULT"` in another shell.
- **Step 5 times out at `--max-memories 5`.** Almost always means
  the `memories_vec` table is corrupt or the index is locked by
  another process. Check `pm2 logs memstem` and the orphan-MCP
  rule (PR #50) before re-running.
- **Step 6 wrote audit rows when you didn't expect them.** Re-read
  the warning above. The default `NoOpJudge` writes one row per
  candidate pair on every run — by design.

### Promoting to a live cutover

The smoke test is a *gate*, not a release. After it passes:

1. Run `memstem hygiene importance` (no flag) and review the
   proposed bumps. If they look right, re-run with `--apply`.
2. Run `memstem hygiene distill` and triage the clusters by hand
   (no automation yet).
3. Run `memstem hygiene dedup-candidates` (full scan, async) and
   review the report.
4. Run `memstem hygiene dedup-judge` (NoOp first, then optionally
   `--enable-llm`) and inspect `dedup_audit`. Do not act on the
   audit table until the resolution PR lands.
