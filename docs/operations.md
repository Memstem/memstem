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

## Post-cleanup operator playbook

This section is the runbook for an operator picking up a vault that
has been live-ingested for a while and needs the periodic-hygiene
pass. The order matters: cleanup before distillation backfill before
verification. The whole loop is idempotent — re-running on a
cleaner vault produces fewer findings, never new mutations.

### 1. Run retro cleanup

Apply the already-shipped Layer 1 dedup + noise rules to records
that pre-date them. **Default is dry-run; review before applying.**

```bash
# Dry-run: prints the plan, mutates nothing.
memstem hygiene cleanup-retro --vault "$VAULT"

# Apply once you've reviewed the plan.
memstem hygiene cleanup-retro --vault "$VAULT" --apply
```

What the writer actually does:

- **Non-skill collisions** (multiple memories with the same
  normalized body hash) → marks the losers `deprecated_by:
  <winner_id>` so default search filters them out. Reversible by
  clearing the field.
- **Skill-involved collisions** → never auto-merge. The writer
  drops a markdown ticket under `vault/skills/_review/` so the
  operator can decide. ADR 0012.
- **Noise hits** (boot echoes, ack-only sessions, transient
  state) → sets `valid_to` so default search excludes them. The
  record stays on disk, recoverable by clearing `valid_to`.

Skip `--noise` or `--dedup` independently with the matching
`--no-*` flag if you want only one of the two passes.

### 2. Backfill session distillations

After cleanup, run the session-distillation writer in backfill
mode so every meaningful session gets a `type=distillation`
companion. Default mode only considers recent (30-day) sessions;
backfill ignores recency and walks the full vault.

```bash
# Make sure your provider key is stored once:
memstem auth set openai sk-...

# Dry-run with the real provider — preview without writing.
memstem hygiene distill-sessions \
  --vault "$VAULT" \
  --backfill \
  --provider openai

# Apply once the preview looks right.
memstem hygiene distill-sessions \
  --vault "$VAULT" \
  --backfill \
  --provider openai \
  --apply
```

Notes:

- `--provider noop` is the safest preview (no LLM calls). NoOp +
  `--apply` is a no-op because the summarizer returns the empty
  string and the applier skips empty summaries.
- Re-running with `--apply` is idempotent: sessions that already
  carry a linked distillation are skipped. Use `--force` only when
  you want to refresh after a prompt or model change.
- `--min-turns 10 --min-words 100` is the meaningfulness gate.
  Lower the thresholds to capture more candidates; raise them to
  drop short transactional sessions.

### 3. Verify success

`memstem hygiene verify` is the operator-facing dashboard for
"did the cleanup + backfill actually land?" It is read-only and
safe to run on a production vault.

```bash
# Human-readable summary.
memstem hygiene verify --vault "$VAULT"

# Machine-readable for CI / monitoring.
memstem hygiene verify --vault "$VAULT" --json-out /tmp/state.json
```

The report covers:

| Section          | Field                                | Meaning |
|------------------|--------------------------------------|---------|
| Total            | `total_memories`                     | Index-resident records |
| By type          | `total/deprecated/valid_to`          | Per-type sub-counts |
| Cleanup state    | `deprecated_total`                   | All records pointed at by `deprecated_by` |
| Cleanup state    | `valid_to_total`                     | All records carrying a `valid_to` (live or expired) |
| Cleanup state    | `active_dedup_groups`                | Collision groups cleanup-retro would still flag |
| Cleanup state    | `active_dedup_to_deprecate`          | Records cleanup-retro would deprecate next run |
| Cleanup state    | `active_dedup_skill_groups`          | Subset of the above that involve a skill (require manual review) |
| Cleanup state    | `noise_drops` / `noise_transients`   | Noise hits cleanup-retro would still flag |
| Cleanup state    | `skill_review_tickets`               | Open tickets under `vault/skills/_review/` |
| Derived records  | `distilled_session_targets`          | Sessions covered by a `type=distillation` link |
| Derived records  | `undistilled_eligible_sessions`      | Sessions that pass the meaningfulness gate but have no companion |
| Health           | `parser_skips`                       | Files skipped during walk because frontmatter validation failed |

A clean vault after the playbook above shows:

- `active_dedup_groups` at zero or only skill-involved groups.
- `noise_drops` at zero, `noise_transients` at zero or one or two
  during the TTL window.
- `undistilled_eligible_sessions` close to zero (modulo new
  sessions that arrived since the last backfill).
- `parser_skips` empty.

### 4. Interpreting remaining findings

After the playbook runs, the residual findings are the operator's
to act on — they are *not* automatic:

- **`active_dedup_skill_groups > 0`**: skill-involved collisions
  never auto-merge. Each one has a ticket under
  `vault/skills/_review/`; review and resolve manually (keep all,
  pick a winner, or dismiss).
- **`noise_transients > 0`**: a non-trivial transient like a
  recent ack-only session. These expire automatically once
  `valid_to` lapses; nothing to do unless the count keeps
  growing, in which case investigate the noise rule.
- **`undistilled_eligible_sessions > 0`**: the next backfill run
  will pick them up. If the count keeps climbing, run the
  backfill on a schedule.
- **`parser_skips` non-empty**: a record was written outside the
  pipeline with malformed frontmatter. The message points at the
  file; fix the file's frontmatter or remove it. Files inside any
  underscore-prefixed directory (`_meta/`, `skills/_review/`,
  `_drafts/`, …) are operator artifacts and never enter
  `parser_skips` — those are skipped silently.

### 5. Skill review ticket workflow

Skill collisions write a ticket per group under
`vault/skills/_review/<timestamp>-<slug>.md`. Each ticket lists
the candidates with their importance, retrieval count, and
update timestamps so the operator can compare without a separate
vault walk.

The tickets are intentionally **plain markdown without
frontmatter** — they are an operator inbox, not vault records.
`Vault.walk()` skips any directory whose name starts with
underscore, so review tickets do not enter the index, do not
appear in search results, and do not produce parser skips.
`memstem hygiene verify` counts the open tickets so the operator
can see at-a-glance how much is on the queue.

To resolve a ticket, edit the relevant skill files manually
(merge content, delete losers, etc.) and remove the ticket
file. There is no first-class `memstem skill-review apply` /
`dismiss` CLI yet — that is a future surface; the current
contract is "read the ticket, edit the vault, delete the
ticket."

### 6. Default ranking policy

`memstem search` defaults to a policy that prefers
**curated/derived** records over **raw** records. Concretely,
each result's RRF score is multiplied by:

```
final = rrf * (1 + alpha * importance) * type_bias[type]
```

The shipped `type_bias` defaults are:

| Type           | Default weight | Effect |
|----------------|---------------:|--------|
| `distillation` | 1.10           | rolled-up summaries lead |
| `memory`       | 1.05           | curated facts |
| `skill`        | 1.05           | curated procedures |
| `project`      | 1.05           | project records |
| `decision`     | 1.05           | decision records |
| `daily`, `person` | 1.00        | neutral |
| `session`      | 0.85           | raw conversation, soft demote |

Bounds are intentionally tight (`[0.85, 1.10]`). A clearly more
relevant raw session still beats a barely-relevant distillation —
the bias breaks ties; it does not override relevance.

To tune the policy, edit `_meta/config.yaml`:

```yaml
search:
  importance_weight: 0.2
  type_bias:
    distillation: 1.20    # lean harder on rollups
    session: 0.70         # demote raw sessions further
    memory: 1.05
    skill: 1.10           # prefer skills above other curated forms
```

Set every entry to `1.0` (or supply an empty mapping `{}`) to
disable the policy entirely and recover the pre-0.10 behaviour.
Per-call overrides are also available via the HTTP `/search`
body (`type_bias` field) and the Python `Search.search` keyword
argument.
