# Dedupe Audit — Policy and Workflow

This document specifies the **read-only dedupe audit** for an existing
MemStem vault: how candidate duplicates are classified, what confidence
and risk attaches to each class, what the audit reports, and the safe
phased workflow for any future cleanup.

The audit ships as `scripts/dedupe_audit_report.py`. It is **strictly
read-only** — it imports only the planner halves of the existing
`memstem.hygiene.cleanup_retro` module, never the writers. Running it
cannot mutate the vault, the index, or any frontmatter.

This is complementary to the already-shipped `memstem hygiene
cleanup-retro` command (ADR 0012 addendum), which covers a single
class (exact body-hash collision) and offers an `--apply` mutation
path. The audit here is broader (multi-class), produces a richer
human + machine report, and has no mutation surface at all.

## Goals

1. Identify candidate duplicates without deleting anything.
2. Group candidates into classes with explicit **confidence** and
   **risk** scores so an operator can review the report and decide
   what (if anything) to act on.
3. Be conservative: when in doubt, the recommended action is
   `manual_review`, not `safe_quarantine_candidate`.
4. Produce two artifacts: a markdown summary and a machine-readable
   JSON report.

## Non-goals

- **No deletion.** This audit never removes a file, never rewrites an
  ID, never edits links or provenance.
- **No automatic merging.** Even when a group is unambiguously a
  byte-identical reingest, the audit only flags it. The existing
  `memstem hygiene cleanup-retro --apply` is the mutation path; that
  command remains the single, intentional way to write
  `deprecated_by` fields.
- **No semantic-similarity judgment.** Cosine-near pairs are out of
  scope here — they are handled by `memstem hygiene dedup-candidates`
  (Layer 2) and `dedup-judge` (Layer 3), per ADR 0012.

## Corpus structure (what the audit inspects)

| Path                         | Type         | Count\*    | Notes                                                       |
|------------------------------|--------------|-----------|-------------------------------------------------------------|
| `memories/claude-code/`      | `memory`     | varies    | UUID-named files; ingested from `~/.claude/projects/...`    |
| `memories/openclaw/<agent>/` | `memory`     | varies    | UUID-named files; ingested from agent workspace             |
| `memories/projects/`         | `project`    | small     | Slug-named files (e.g. `home-ubuntu-ari.md`); curated       |
| `sessions/`                  | `session`    | largest   | UUID-named; one per ingested session transcript             |
| `distillations/<source>/`    | `distillation` | medium  | UUID-named; LLM digest records — derivative by design       |
| `daily/<agent>/`             | `daily`      | small     | Date-named (`2026-04-15.md`); date-bucketed logs            |
| `skills/<agent>/...`         | `skill`      | medium    | Mixed UUID and slug paths; high-leverage, never auto-merge  |

\*Exact counts are reported by the audit at run time.

The fields available for dedupe analysis in every record's frontmatter:

- `id` — UUID for most records; project records use a slug-derived
  filename but still carry a UUID `id` field.
- `type` — one of the values above.
- `source` — adapter name (`claude-code`, `openclaw`, `hygiene-worker`,
  `human`).
- `provenance.source`, `provenance.ref`, `provenance.ingested_at` —
  pointer to the originating file path (or virtual ref like
  `session-distillation:<id>` or `project:<slug>`).
- `title`, `tags`, `links`, `prerequisites`.
- `created`, `updated`, `valid_from`, `valid_to`.
- `importance`, `confidence`, `embedding_version`.
- `deprecated_by` — already present in the schema; audit treats
  records with this set as already-collapsed and excludes them.

The body hash used for dedupe is `core.dedup.normalized_body_hash` —
SHA-256 over a whitespace-normalized, lowercased body. The same hash
function the live pipeline uses for Layer 1.

## Dedupe classes

Each candidate group is classified into exactly one of the classes
below. Confidence reflects how likely the group is a real duplicate;
risk reflects what happens if it's collapsed by mistake.

### Class A — Exact body-hash collision, same type

**Signal:** Two or more records with the same normalized body hash and
the same `type`.

**Confidence:** HIGH — bodies are byte-equivalent modulo whitespace.

**Risk:** LOW for `memory`, `daily`, `session`, `distillation`.
**HIGH** for `skill`, `project`. The high-risk subtypes get a separate
class below.

**Recommended action:** `safe_quarantine_candidate` for the low-risk
subtypes — these are the prototypical reingest case from before
Layer 1 dedup landed. The `cleanup-retro` writer handles them by
marking the loser with `deprecated_by`, leaving the file on disk.

### Class B — Reingest of the same source ref

**Signal:** Two or more records with **same `provenance.ref`** AND
**same body hash**, different `id`s.

**Confidence:** HIGH — the source file was re-ingested; the resulting
records are functionally identical.

**Risk:** LOW. This is the cleanest reingest signature.

**Recommended action:** `safe_quarantine_candidate`. Often a strict
subset of Class A; recorded separately so the operator can see how
much of the body-hash damage is attributable to known re-ingest.

### Class C — Source-updated (NOT a duplicate)

**Signal:** Two or more records with **same `provenance.ref`** but
**different body hashes**.

**Confidence:** HIGH that this is **not** a duplicate. The source file
was edited between ingests and both versions of the content are now
in the vault.

**Risk:** HIGH if treated as a duplicate — collapsing them destroys
the historical version.

**Recommended action:** `keep_all`. Surfaced in the report so the
operator knows the source is being re-ingested under the same `ref`;
if they want only the latest, that is a separate decision for the
ingest pipeline, not for this audit.

### Class D — Title-equivalent + body-hash equal

**Signal:** Two or more records with the same body hash AND the same
(case-insensitive, whitespace-normalized) title.

**Confidence:** VERY HIGH — both content and title agree. Strong
confirming signal beyond Class A.

**Risk:** LOW (same risk profile as Class A by subtype).

**Recommended action:** `safe_quarantine_candidate`. Reported as a
high-confidence subset of Class A.

### Class E — Title-equivalent + body-hash different

**Signal:** Two or more records with the same title but different
body hashes.

**Confidence:** LOW — same title can mean "same source updated", "two
unrelated daily logs that happen to share a heading", or "near
duplicate written at different times".

**Risk:** HIGH if collapsed automatically.

**Recommended action:** `manual_review`. The audit lists candidate
groups but never recommends quarantine.

### Class F — Cross-type body-hash collision

**Signal:** Records with the same body hash but **different `type`s**
(e.g. one `memory` and one `distillation`, or one `memory` and one
`session`).

**Confidence:** Mixed — same bytes, but the records play different
roles in the system.

**Risk:** HIGH. A distillation that happens to copy a chunk of source
verbatim is intentional, not a duplicate. A project record sharing a
hash with anything else is even more suspect.

**Recommended action:** `manual_review`. Never `safe_quarantine_candidate`.

### Class G — Skill collisions

**Signal:** Body-hash collision involving any record with `type:
skill`.

**Confidence:** Same as the body-hash signal.

**Risk:** VERY HIGH per ADR 0012. An incorrect auto-merge of two
skills causes hours of debugging across every workflow that uses
them.

**Recommended action:** `manual_review` and route to the
`vault/skills/_review/` queue. The audit lists these but expects the
operator to review each one by hand. The existing `cleanup-retro
--apply` already implements this routing; the audit only reports.

### Class H — Derived-record collisions (defensive)

**Signal:** Body-hash collision among records of `type: distillation`
or `type: project`.

**Confidence:** Variable.

**Risk:** HIGH. Project and distillation records are curated rollups;
duplicates likely indicate a hygiene-worker bug, not a regular
reingest, and should be diagnosed before being collapsed.

**Recommended action:** `manual_review`.

## Confidence × risk → recommended action

The matrix the audit uses:

| Confidence | Risk      | Action                             |
|------------|-----------|------------------------------------|
| HIGH       | LOW       | `safe_quarantine_candidate`        |
| HIGH       | HIGH      | `manual_review` (often `keep_all`) |
| LOW        | any       | `manual_review`                    |
| any        | very high | `manual_review`                    |

`safe_quarantine_candidate` does **not** mean "delete". It means "a
reasonable next step is to mark the loser with `deprecated_by` via
the existing `cleanup-retro --apply` flow, which keeps both files on
disk and is reversible by editing the loser's frontmatter".

## Winner selection

For groups recommended for quarantine, the audit uses the same winner
heuristic as `cleanup_retro.select_winner` (ADR 0012 addendum):

1. Highest `importance` (None treated as 0.5).
2. Most retrievals from `query_log` (proxy for "in active use").
3. Most-recently-`updated` (proxy for "the active-pipeline copy").
4. Lexicographically smallest `id` (deterministic tiebreak).

The audit annotates each group with `coin_flip = true` when only the
final tiebreak distinguished the winner. Coin-flip groups are still
candidates for quarantine (the bodies are equal by definition), but
the operator can spot-check them.

## Output

The audit writes two files:

1. `dedupe-audit-<timestamp>.md` — human-readable report. Per-class
   summary table, then the top groups in each class with member
   IDs, paths, types, provenance refs, ingest timestamps, similarity
   rationale, recommended winner, and recommended action.
2. `dedupe-audit-<timestamp>.json` — machine-readable. Same data as
   the markdown plus the full member list for every group; suitable
   for diffing across runs or driving a review UI.

By default both files land in `_meta/audits/` inside the vault. The
script accepts `--out-dir` to override.

## Phase-1 refinement (the gate before any cleanup)

The 8-class audit above is a **survey**, not a cleanup pool. Multiple
classes can fire on the same underlying duplicate set (e.g. one set is
typically in A, B, and D simultaneously), and Class A can include
false positives where the same body lives at genuinely different
source paths. The Phase-1 selector,
`scripts/dedupe_phase1_select.py`, takes the audit JSON and emits a
strictly-filtered manifest of groups safe enough for near-mechanical
quarantine review.

A group enters the Phase-1 manifest only if **all** of these hold:

1. Same record `type` for every member.
2. Same `provenance.source` for every member.
3. Same `provenance.ref` for every member.
4. Same normalized body hash for every member.
5. No member already carries `deprecated_by`.
6. `type` is not in `{skill, project, distillation}`.
7. The group is single-class (no cross-type collisions).
8. Winner selection is unambiguous (`coin_flip == false`).
9. No mixed source-root paths (covered transitively by 2 + 3).

Phase-1 starts from the audit's Class B (same provenance.ref + same
body hash) — that is the only class where ref-equality is already
enforced. Class A groups that aren't also Class B are exactly the
false-positive pattern (same body, different source location):

| Pattern | Why excluded from Phase 1 |
|---------|---------------------------|
| Same-named source file (e.g. `MEMORY.md`) ingested from two different parent directories | Two intentionally separate source files that currently happen to share content; collapsing one loses a tracked source. |
| Two distinct Claude Code session JSONLs that happen to contain a trivial shared body (e.g. `"hi"`) | Different sessions; identity is the file pointer, not the text. |

The selector writes:

- `<vault>/_meta/audits/phase1-manifest-<ts>.json` — machine-readable,
  one entry per duplicate set, no class-overlap inflation.
- `<vault>/_meta/audits/phase1-report-<ts>.md` — human-reviewable.

Run with `--show-plan` to also print the read-only quarantine plan
(what `deprecated_by` markers a future apply step would write).

The selector has no `--apply` flag; quarantine-time mutation goes
through the existing `memstem hygiene cleanup-retro --apply` path,
operated by the user after they've reviewed the manifest.

## Recommended cleanup workflow

The audit produces candidates; this section is the safe operational
plan for acting on them later. **Do not perform these steps as part
of running the audit.** They are described here so the audit's
recommendations are interpretable.

### Step 0 — Snapshot first

Before any apply pass:

```bash
cp -a /home/ubuntu/memstem-vault /home/ubuntu/memstem-vault.audit-snap-$(date +%Y%m%dT%H%M%SZ)
sqlite3 /home/ubuntu/memstem-vault/_meta/index.db ".backup '/home/ubuntu/memstem-vault/_meta/backups/index-$(date +%Y%m%dT%H%M%SZ).db'"
```

Both the canonical markdown tree and the index are snapshotted. The
markdown tree is the canonical store; the index is rebuildable from
it via `memstem reindex`.

### Step 1 — Phase A: exact body-hash, low-risk classes only

Run:

```bash
memstem hygiene cleanup-retro --no-noise --json-out /tmp/phase-a.json
```

Default is dry-run. Review the report. The classes covered by this
command are A, B, D when limited to non-skill, non-project,
non-distillation types.

When ready:

```bash
memstem hygiene cleanup-retro --no-noise --apply --json-out /tmp/phase-a-applied.json
```

This sets `deprecated_by` on the losers. Files stay on disk. Default
search filters them via the existing `deprecated_by` filter in
`core/search.py`.

### Step 2 — Validation

```bash
memstem search "<a query you know returns the winner today>"
```

Confirm the winner is still surfaced and the loser is filtered.
Re-run the audit script:

```bash
python3 scripts/dedupe_audit_report.py
```

The Class A / B / D group counts should drop sharply. Classes C, E,
F, G, H are unchanged (Phase A doesn't touch them).

### Step 3 — Phase B: skill review queue

Skill collisions wrote review tickets to `vault/skills/_review/` in
Phase A. Walk each ticket by hand:

```bash
ls /home/ubuntu/memstem-vault/skills/_review/
```

For each ticket: read it, decide whether to apply (mark loser
`deprecated_by`), dismiss (no action; both stay), or do something
custom. The CLI (`memstem skill-review apply <ticket>` /
`dismiss <ticket>`) is part of PR-E in the ADR 0012 phasing — until
that lands, edit the loser's frontmatter by hand and delete the
ticket file. Either way the operation is reversible.

### Step 4 — Phase C: manual review queue

Classes E, F, H from the audit. The audit emits the candidate list;
the operator reviews each group, decides per case, and edits
frontmatter directly when they want to deprecate. Nothing automated.

Class C (source-updated) is **not** in the manual review queue — it
is reported as `keep_all` and the operator does not act on it from
this audit. If the operator wants to change ingest behavior for
re-edited source files, that is a separate decision against the
adapter, not a vault cleanup.

### Step 5 — Rollback plan

Every action in this workflow is reversible by editing canonical
markdown:

- `deprecated_by` cleanup: open the loser's `.md` file, remove the
  `deprecated_by:` line in frontmatter, save. Run `memstem reindex`.
- Snapshot rollback (worst case): delete the working vault, restore
  the snapshot directory copied in Step 0, run `memstem reindex`.
- Index-only rollback: drop `_meta/index.db`, copy back the
  pre-apply backup from `_meta/backups/`, restart the daemon.

The `dedup_audit` table holds an append-only log of every applied
verdict (`judge="layer1-retro"` for the Phase A pass). It is the
source of truth for "which deprecate decisions were applied and
when".

## Idempotence

- The audit script can be re-run any time. It always reads fresh state
  and produces a fresh report.
- Records already carrying `deprecated_by` are excluded from candidate
  groups — they're already collapsed and revisiting them only inflates
  the report.
- Running `cleanup-retro --apply` after a Phase A pass is also
  idempotent (the underlying planner excludes deprecated records).
