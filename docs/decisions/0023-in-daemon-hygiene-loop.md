# ADR 0023 — In-daemon hygiene loop

**Status:** Accepted (2026-05-19)
**Supersedes:** none
**Related:** ADR 0008 (importance), ADR 0012 (dedup), ADR 0020 (session distillation), ADR 0021 (project records)

## Context

Memstem v0.10 ships four hygiene tasks as one-shot CLI commands:

- `memstem hygiene distill-sessions` — write `type: distillation` records (ADR 0020)
- `memstem hygiene dedup-judge` — LLM-judge near-duplicate candidate pairs (ADR 0012 Layer 3)
- `memstem hygiene importance` — bump `importance` on recently retrieved memories (ADR 0008 PR-C)
- `memstem hygiene project-records` — aggregate Claude Code project tags into `type: project` records (ADR 0021)

These commands were designed to run on a cron or systemd timer, scheduled outside the daemon. In practice no scheduling layer was ever set up — the fleet has `distill-sessions` last firing on 2026-05-05 (one-shot backfill, never since), `dedup-judge` last on 2026-05-02 (backfill, never since), `importance` never run, `project-records` last on 2026-04-29.

Every Memstem instance audited (Ari, E1, three UltraClaw customer containers) shows the same pattern. The ingestion + embedding pipeline works; the curation pipeline is dormant.

## Decision

Run the hygiene tasks as a fourth background `asyncio` task inside the `memstem daemon` process, alongside the adapter watchers, embed-worker pool, and HTTP server.

The loop:

1. Wakes every `poll_interval_seconds` (default 60s) and checks each stage's interval.
2. For each stage whose interval has elapsed since the last run, acquires a `hygiene_state` row-lock keyed `running_since:<stage>`, runs the work via `asyncio.to_thread`, then writes `last_run:<stage>` and clears the lock.
3. Wraps each stage in `try/except` so one failure does not kill the loop or any other stage.
4. Reads `cfg.hygiene.loop_enabled`; if False, the loop does not start at all.

The existing CLI hygiene commands continue to work for manual debug/backfill runs. Before doing work, each CLI command checks `running_since:<stage>` and refuses with a clear message if held — the loop and the CLI cannot compete.

## Alternatives considered

**Cron / systemd timers.** The original design. Rejected because:

- Adds an out-of-process scheduling layer that has to be installed and maintained per host. The empirical record is that this layer never gets set up.
- Cron failures email; PM2 logs aggregate. We already have PM2 ownership of the daemon — keeping hygiene in-process keeps observability in one place.
- Each cron invocation pays cold-start cost (Python interpreter + index open + LLM client init). In-process amortizes that across cycles.

**Per-stage PM2 entries.** One PM2 process per hygiene stage that exits and respawns on a `cron_restart` schedule. Rejected because the cold-start cost is even worse and PM2's `cron_restart` is intended for "restart at this time", not "run this command on schedule".

**HTTP-triggered hygiene.** A `POST /hygiene/run/<stage>` endpoint that an external scheduler hits. Rejected for v0.11 — equivalent observability burden to cron, more moving parts. May be added in a future ADR if external orchestration becomes valuable.

## Configuration

New `HygieneConfig` fields, all with safe defaults:

| Field | Default | Purpose |
|---|---|---|
| `loop_enabled` | `True` | Master switch. Set False on customer containers where the customer's API key would be charged. |
| `poll_interval_seconds` | `60` | How often the loop wakes to check stage timers. |
| `distill_interval_seconds` | `21600` (6h) | Cadence for `distill-sessions`. |
| `dedup_interval_seconds` | `86400` (24h) | Cadence for `dedup-candidates` + `dedup-judge`. |
| `importance_interval_seconds` | `3600` (1h) | Cadence for `importance`. |
| `project_records_interval_seconds` | `86400` (24h) | Cadence for `project-records`. |
| `distill_max_per_cycle` | `50` | Cap on items processed per distillation cycle. Prevents a cold vault from running thousands of LLM calls on the first tick. |
| `dedup_max_per_cycle` | `100` | Cap on candidate pairs judged per dedup cycle. |
| `summarizer_provider` | `"openai"` | Provider for distillation/project_records. `"noop"` skips the LLM call (records cycle but writes no distillations). |
| `summarizer_model` | `None` | Optional model override. Provider-default if unset. |
| `judge_provider` | `"noop"` | Provider for dedup-judge. NoOp logs `UNRELATED` audit rows for inventory without calling an LLM (safer default since dedup-judge writes to the audit table). |

## Lock semantics

`hygiene_state` already exists as a small `(key TEXT PRIMARY KEY, value TEXT NOT NULL)` table used by `importance.py` for cursor tracking. Two new key namespaces are introduced:

- `last_run:<stage>` — RFC 3339 timestamp of the most recent successful completion.
- `running_since:<stage>` — RFC 3339 timestamp set when a runner acquires the stage. Cleared on completion or on stale-detection (older than 1h is treated as crashed and cleared).

The loop and the CLI use the same helpers in `memstem.hygiene.state`:

```python
acquire_stage_lock(db, stage, max_age_seconds=3600) -> bool
release_stage_lock(db, stage) -> None
get_last_run(db, stage) -> datetime | None
set_last_run(db, stage, ts: datetime) -> None
```

`acquire_stage_lock` returns False if another runner currently holds the lock and the lock is not stale.

## Failure isolation

Each stage runs as:

```python
try:
    if not acquire_stage_lock(db, stage):
        logger.info("hygiene[%s]: skipped — another runner holds the lock", stage)
        return
    try:
        await asyncio.to_thread(self._run_stage, stage)
        set_last_run(db, stage, datetime.now(UTC))
    finally:
        release_stage_lock(db, stage)
except Exception:
    logger.exception("hygiene[%s]: cycle failed", stage)
```

A `sqlite3.InterfaceError`, an OpenAI timeout, or a malformed cluster in one stage cannot take down ingestion, the embedder, or the other hygiene stages.

## Observability

The `/health` endpoint gains a `hygiene` block:

```json
{
  "status": "ok",
  "version": "0.11.0",
  "vault": "/home/ubuntu/memstem-vault",
  "embedder": true,
  "hygiene": {
    "loop_enabled": true,
    "last_run": {
      "distill_sessions": "2026-05-19T18:00:00Z",
      "dedup_judge": "2026-05-19T00:00:00Z",
      "importance": "2026-05-19T19:00:00Z",
      "project_records": "2026-05-19T00:00:00Z"
    },
    "running": []
  }
}
```

`running` lists stages currently mid-cycle (read from `running_since:*` rows). Useful for fleet monitoring.

## Rollout

Native PM2 installs (Ari, Daymond's ultra-openclaw, Skyler's e1-new):
`pipx upgrade memstem && pm2 restart memstem`. Loop starts automatically.

UltraClaw customer containers (halston, ultra, sevenjade, et al.):
The `templates/memstem/config.yaml.template` in the `ultraclaw` repo gains `hygiene: { loop_enabled: false }`. After rebuilding `ultraweb/memstem:latest` from the new image and re-running `bin/attach-memstem.sh <slug>`, customer containers still ingest and embed but do not run hygiene. When self-hosted summarization comes online, flip the customer template to `loop_enabled: true` and re-run `attach-memstem.sh`.

## Tests

- `tests/hygiene/test_loop_intervals.py` — interval gating: a stage with `last_run` newer than its interval is skipped; a stage with `last_run = None` runs immediately.
- `tests/hygiene/test_loop_lock.py` — lock acquisition: two runners cannot both hold the same stage. Stale locks (`> max_age_seconds`) are cleared.
- `tests/hygiene/test_loop_failure_isolation.py` — a stage that raises does not affect other stages.
- `tests/hygiene/test_cli_lock_check.py` — `memstem hygiene distill-sessions` refuses cleanly when the loop holds the lock.
- `tests/test_health_endpoint.py` — `/health` includes the `hygiene` block.
