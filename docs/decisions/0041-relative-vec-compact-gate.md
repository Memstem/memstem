# 0041 — vec compaction gate scales with the vault

- Status: accepted
- Date: 2026-09-11
- Supplements: 0036 (vec table compaction), 0039 (daily threshold-gated cadence), 0040 (atomic swap)

## Context

ADR 0036 gated the `vec_compact` hygiene stage on two thresholds: occupancy
below `vec_compact_max_occupancy` (0.6, raised to 0.8 by ADR 0039) **and** at
least `vec_compact_min_dead_slots` dead slots (25,000). The absolute floor was
chosen on the vault the incident happened on — 125k live vectors at the time,
185k today — where 25k slots is ~400 MB of dead scan per query and roughly
one day of churn. It was a deliberate cost/benefit line while compaction
still held the writer lock for the whole rebuild (a search freeze of 47 min
on 2026-09-02, 20 min nightly on 2026-09-03): below ~400 MB the dead slots
sit in page cache and cost less than the freeze would.

The floor was never re-derived for smaller vaults, and on them it disables
the stage outright. Measured on 2026-09-10/11 across the fleet (all on
0.22.0, weekly checks):

| vault | live | dead | dead % | stage decision |
|---|---|---|---|---|
| snape-server | 30,816 | 19,360 | 39% | skip — dead < 25,000 |
| Damon | 17,930 | 12,790 | 42% | skip |
| E1 | 13,534 | 16,162 | 54% | skip |
| ramc (ultraclaw) | 69,100 | 10,772 | 14% | skip (correctly) |

On a 50k-slot table, 25k dead is 45% dead. The stage on snape-server logged
a skip every Wednesday for three weeks while the customer watched the table
grow from 12% to 39% dead, then ran the CLI by hand and wrote his own cron.
The gain he measured was real (39% fewer slots scanned) and, at that size,
not felt (search 1.5–2.1 s before, ~2.4 s after). The cost side has since
changed: with ADR 0040 the rebuild is lock-free and the swap holds the lock
for ~10 s, so a small-vault compaction costs seconds of slightly slower
search rather than a freeze.

## Decision

1. **`vec_compact_min_dead_slots` default 25,000 → 1,024** — one full vec0
   chunk (16 MB at 4096-dim). Below one chunk of dead slots a rebuild can
   reclaim at most one chunk, so the floor's only job is to keep
   single-chunk vaults from rebuilding for nothing.
2. **The occupancy gate is the threshold at every size.** Compact when more
   than 20% of slots are dead (`vec_compact_max_occupancy` 0.8, unchanged).
   On the 185k-vector vault this needs ~46k dead slots, which already bound
   before the floor did, so large vaults behave exactly as before.
3. Skip log lines carry the dead-slot percentage so an operator reading the
   daemon log sees the fraction, not just counts.

Operators who want the old behaviour set `hygiene.vec_compact_min_dead_slots:
25000` in `config.yaml`; nothing else changes.

## Consequences

- Small vaults compact themselves once they pass 20% dead: snape-server,
  Damon and E1 are picked up on the first daily check after upgrading.
- Cost on a small vault: a lock-free build of a few hundred MB plus a
  ~10 s swap. On the 4-vCPU snape-server that is roughly a minute of
  slightly slower searches once a day at most.
- A 0.22.0 daemon (blocking compaction) that pins this floor low would
  freeze search for the rebuild. The new default ships only with the
  ADR 0040 code, in the same release.
- Not addressed here, noted for follow-up: a live session re-embeds its
  whole body every time it settles although only the tail changed (one
  conversation produced 1,783 dead slots in three hours on snape-server).
  ADR 0036 rejected per-chunk reuse on the assumption that churned records
  change throughout; append-only sessions do not. Reusing unchanged leading
  chunks would cut churn and embedding spend at the source.
