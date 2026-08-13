# 0034 — Claude Code project tag derives from the session's working directory

Status: **Accepted — implemented**
Date: 2026-08-13
Supersedes: none
Related: 0021 (project records), 0005 (pull-based ingestion)

## Context

The Claude Code adapter tagged each session with the directory Claude Code
filed it under ([adapters/claude_code.py](../../src/memstem/adapters/claude_code.py)):

```python
project_dir = path.parent.name
tags = [project_dir.lstrip("-")] if project_dir.startswith("-") else []
```

Claude Code names that directory after the **cwd the CLI was launched from**,
encoded by replacing `/` with `-`. The name is fixed at process start; `cd`-ing
during the session does not move the file. So the tag records where the user
*started*, not what they *worked on*.

That distinction is invisible for a user who launches from inside each repo, and
fatal for one who launches from `$HOME` and navigates — a pattern that any
"pick a project first, then work in it" instruction produces. On the vault that
surfaced this, **625 of 633 session transcripts carried the single tag
`home-ubuntu`**, spanning roughly forty unrelated projects.

The damage is not limited to search filtering. ADR 0021 defines a project as
"a Claude Code project tag with ≥ 2 sessions" and aggregates each into a
`type: project` rollup record with a `Latest known state` section. With one tag
swallowing everything, that machinery produced a single record linking **948
sessions**, titled after whichever theme the summarizer found most salient,
whose `Latest known state` described infrastructure decommissioned three months
earlier. The feature was working exactly as designed on inputs that were wrong,
and it carries `importance: 0.85` — above session distillations — so the bad
record outranked its own sources.

Two further defects sat in the same two lines:

**Nested transcripts lost their tag entirely.** Subagent and workflow
transcripts are written to `<encoded-cwd>/subagents/*.jsonl` and
`<encoded-cwd>/wf_<id>/*.jsonl`. For those, `path.parent.name` is `subagents`
or `wf_2492d8d5-e48`, neither of which starts with `-`, so `tags` came out
empty. 143 transcripts in the observed vault were affected.

**Scratch directories minted projects.** Any directory Claude Code was launched
from — `/tmp` paths, one-off review dirs — became a project tag of equal
standing to a real one.

The fix is available for free: **every entry in a session JSONL carries its own
`cwd` field**. The directory the user selected has been recorded on disk all
along, per message; only the grouping key ignored it.

## Decision

Derive the project tag from the cwds recorded *inside* the transcript, falling
back to the launch directory.

1. **Resolve the launch directory by walking up** to the nearest ancestor whose
   name starts with `-`, instead of trusting `path.parent`. This is what makes
   nested subagent and workflow transcripts resolve to their parent session's
   project rather than to `subagents`.
2. **Collect `cwd` counts** while parsing, along with the last entry index each
   was seen at.
3. **Rank candidates**, where a candidate is any recorded cwd that differs from
   the launch directory, by entry count, ties broken by most recent use.
4. **Fall back to the launch directory** when there are no candidates — the
   session never left where it started, or the transcript predates per-entry
   `cwd`. This preserves existing behaviour for every launched-in-place user.

Rejected: **ranking by raw frequency**, which reproduces the bug. The launch
directory is usually the *most* common cwd — in a representative transcript, 17
entries against 3 for the project actually worked in — because orientation,
planning, and cross-cutting commands run before and between the `cd`. Excluding
the launch directory before ranking is the whole point; a plain mode does not.

Tags keep their existing encoded shape (`home-ubuntu-projects-foo`), so ADR 0021
slugs, vault filenames under `memories/projects/`, and any saved query continue
to work unchanged.

## Consequences

**Sessions regroup.** On the vault that motivated this, tags go from 21 buckets
(one holding 625 transcripts) to **74**, with the largest legitimate project at
116. Project records become per-project instead of one conflated rollup, and
`Latest known state` starts describing one piece of work.

**Existing project records are stale on upgrade, not wrong-by-construction.**
`memories/projects/<slug>.md` files keyed to old tags persist until
regenerated. Deployments carrying meaningful history should re-run project
records after re-ingesting sessions. The conflated record should be deleted by
hand; nothing rewrites it in place.

**Directories deeper than the project root still tag separately.** A session run
entirely in `projects/tpv-cloud/api` tags as that path, not as `tpv-cloud` —
6 of 602 transcripts in the observed vault. Normalizing to a project root needs
a notion of where roots live, which is per-installation config; deferred until
there is evidence it matters. It is a strictly smaller error than the one being
fixed: a real subdirectory rather than an unrelated project.

**Parsing cost is unchanged** — two dict writes per entry in a loop already
running, no extra file reads.

**No migration ships with this change.** Re-tagging existing transcripts is
re-ingestion, which the reconcile sweep already performs; operators who want it
immediately can force a reconcile. The index is derived and rebuildable
(ADR 0002), so no data is at risk.
