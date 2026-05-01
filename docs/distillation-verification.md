# Verifying session distillation + project records on your vault

> Companion to ADRs 0020 (session distillation) and 0021 (project
> records). This doc walks an operator through the post-cutover
> verification workflow on a live MemStem vault. Read the
> [recall-models guide](./recall-models.md) first — it covers the
> model choices these commands take.

The new commands are CLI-driven and idempotent. The recommended
order is: dry-run with NoOp → dry-run with the real provider →
apply → spot-check the output → run the eval harness.

## 1. Pre-flight

Confirm the writer can see your vault and the providers you plan to
use:

```bash
memstem doctor
memstem auth show openai   # if using OpenAI
ollama list                # if using Ollama; chat model + embedder
```

If you're on OpenAI, store the key once so cron / PM2 / headless
shells inherit it:

```bash
memstem auth set openai sk-...
```

If you're on Ollama, pull the recommended model (one-shot, ~5GB):

```bash
ollama pull qwen2.5:7b
```

## 2. Dry-run the candidate set (free, NoOp)

The dry-run + NoOp combination is the safest preview — it walks
your vault, shows you which sessions and projects qualify, and
writes nothing. NoOp returns empty summaries, so every proposal is
listed but skipped.

```bash
memstem hygiene distill-sessions --backfill   # NoOp, dry-run
memstem hygiene project-records               # NoOp, dry-run
```

The output lines tell you, per session/project:

- `✓` — would produce a distillation/project record
- `·` — skipped (empty summary, NoOp default)
- `M` — manual:true preserved (project records only)

If the candidate set looks wrong (too many trivial sessions, missing
projects, etc.), tune the thresholds:

- `--min-turns 15 --min-words 200` — stricter session threshold
- `--min-sessions 3` — only group projects with 3+ sessions

## 3. Dry-run with a real provider (cheap)

Switch to your chosen provider and re-run dry-run. This actually
calls the LLM but writes nothing to the vault. The cache table
(`summarizer_cache`) does get populated, so a follow-up `--apply`
will be free.

```bash
# OpenAI:
memstem hygiene distill-sessions --backfill --provider openai

# Ollama:
memstem hygiene distill-sessions --backfill --provider ollama
```

Cost note: a dry-run pass over Brad's ~356 Claude Code sessions on
`gpt-5.4-mini` runs about $1 with all cache misses; subsequent
runs hit the cache and cost nothing.

## 4. Apply

Once the dry-run output looks good, persist:

```bash
memstem hygiene distill-sessions --backfill --provider openai --apply
memstem hygiene project-records --provider openai --apply
```

Order matters: distill sessions first so the project writer sees
clean inputs. The project writer prefers session distillations over
raw transcripts when both are available.

Each command prints a summary at the end (`written`, `updated`,
`skipped`, errors). Re-runs are idempotent — sessions whose
distillation already exists are skipped, and project records whose
source set hasn't changed short-circuit via the cache.

## 5. Spot-check quality (manual inspection)

Pick a known project and read the generated record:

```bash
# Project record:
cat ~/memstem-vault/memories/projects/home-ubuntu-woodfield-quotes.md

# A session distillation that contributed to it:
ls ~/memstem-vault/distillations/claude-code/ | head -5
cat ~/memstem-vault/distillations/claude-code/<session-id>.md
```

What to look for (if any of these fail, see "What to do if quality
is poor" below):

- **Title** — Is the canonical project name the way you'd phrase it?
  ("Woodfield Country Club — e-bike & golf cart tracking", not
  `home-ubuntu-woodfield-quotes`.)
- **Entity coverage** — Does the summary mention the people,
  organizations, and specific deliverables you'd expect?
- **Length** — Is the body roughly a paragraph + 4 short sections,
  not a 2-page essay or a one-liner?
- **Grounded** — Does each claim trace back to source material in
  the linked sessions/distillations?
- **No hallucinated facts** — Are the deliverables, decisions, and
  dates accurate?

## 6. Run the eval harness

The eval harness measures retrieval quality on a fixed query set.
Run it before your first `--apply` to capture the baseline, then
re-run after to see the lift:

```bash
# Baseline:
python -m memstem.eval.harness --queries eval/queries.yaml --json-out /tmp/eval-before.json

# After apply (re-run):
python -m memstem.eval.harness --queries eval/queries.yaml --json-out /tmp/eval-after.json

# Diff the JSON for the lift on the project_* queries.
```

The Woodfield-shape queries (`project_woodfield_ebike_video`,
`project_aerial_demo_revision`, `project_recent_client_work`,
`project_distillation_ranks_above_raw`) are the ones designed to
exercise the new pipeline. Substring matchers in the queries assume
your vault contains a Woodfield project — replace the entity
substrings with terms from your own projects if needed (the
`expect.body_contains` / `expect.path_contains` lists are easy to
edit).

## 7. What to do if quality is poor

The first lever is the prompt template, not the model. Both
templates live next to the code:

```
src/memstem/prompts/distill_session.txt
src/memstem/prompts/distill_project.txt
```

Tweak, then re-run with `--force` to regenerate against the new
template:

```bash
memstem hygiene distill-sessions --backfill --provider openai --force --apply
memstem hygiene project-records --provider openai --force --apply
```

The summarizer cache key is the SHA-256 of the full prompt, so any
template change invalidates the relevant cache rows automatically.

If template tuning doesn't fix it, climb the upgrade ladder in
[`recall-models.md`](./recall-models.md):

- `gpt-5.4-mini` → `gpt-5.4` → `gpt-5`
- `qwen2.5:7b` → `qwen2.5:14b` → `qwen2.5:32b`

## 8. Protecting hand-edited project records

If you hand-edit a project record (the LLM's framing is wrong, you
want to add context the source sessions didn't capture, etc.), set
`manual: true` in the record's frontmatter:

```yaml
---
id: ...
type: project
title: My Better Title
manual: true        # <- this line
...
---

# My Better Title

Hand-curated body...
```

Future `memstem hygiene project-records` runs will refresh `links`
and `updated` (so the link map stays current as new sessions land)
but preserve the body. `--force` overrides `manual: true` if you
want to regenerate later.

## 9. Routine maintenance

After the initial backfill, both commands are safe to run on a
schedule (e.g. nightly via cron / PM2 / `memstem schedule`):

```bash
memstem hygiene distill-sessions --provider openai --apply
memstem hygiene project-records --provider openai --apply
```

Without `--backfill`, distill-sessions only scans sessions updated
in the last 30 days (configurable via `--recency-days`). Project
records always scan all qualifying tags but short-circuit when the
source set hasn't changed.

Steady-state cost is dominated by genuinely new sessions
(typically < $1/month at Brad's pace). Repeat runs over unchanged
data hit the cache and cost nothing.
