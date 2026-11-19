# ADR 0057: Handoff notes as searchable records

Status: proposed. Date: 2026-11-18.

## Context

Agents end long work by writing a handoff note (`HANDOFF.md`,
`HANDOFF-2026-11-13-topic.md`, `docs/pre-handoff-gate3.md`) into the project they
worked in. The note states what was decided and what is still open, in one place and
in the author's words. Today none of them are records: no adapter looks in project
directories. A question about a decision is answered, when it is answered, by the
session that happened to say it or by that session's distillation.

Measured on one install (2026-11-18, 20 questions written from 10 handoff notes):
the answer was in the first three results for 16 and was the first result for 7. The
note itself was never returned, because 0 of 143 distinct notes on that host were
indexed. A second install had 1 of 21 indexed.

Three properties of the files shape the design:

- **They sit in very large trees.** The two project roots on that host hold 108,000
  directories. A recursive inotify watch on them is not affordable; a pruned walk
  takes 1.4 seconds.
- **Git worktrees multiply them.** 300 matching files were 93 notes. One note had 34
  copies across linked worktrees, in two versions. Indexing every copy would fill the
  first results with near-identical records, and an edit in one worktree would leave
  the older copies searchable beside it.
- **Some are rolling.** A project may keep one long note that every session rewrites,
  or write a fresh copy of it into a new directory each time.
- **File times lie in a checkout.** Git stamps every file with the time of the
  checkout, so a worktree made today from an old branch holds the oldest text under
  the newest modification time.

## Decision

Add an opt-in `handoff` adapter that polls configured roots for handoff notes and
emits one `memory` record per note.

**Discovery.** Each root is walked with pruning: hidden directories (names starting
with a dot), dependency and build directories (`node_modules`, `site-packages`,
`venv`, `__pycache__`, `dist`, `build`, `target`, `vendor`), any `exclude_dirs` from
config, and anything deeper than `max_depth` (default 8). Symlinked directories are
not followed. A file is a note when its name matches one of `patterns` (default
`*handoff*.md`, case-insensitive) and none of `exclude_patterns` (default
`*template*`), and is at most `max_file_bytes` (default 2 MB). A root that is itself
hidden, or inside a hidden directory, is still walked: pruning applies to what is
found below a root, not to what the operator named.

**One record per note, not per copy.** Copies are grouped into a note by identity:

- Inside a git repository found at or below the root, identity is the path relative
  to the working tree, joined to the *main* working tree. A linked worktree is
  recognised by its `.git` file (`gitdir: <main>/.git/worktrees/<name>`), read
  directly. So `app-wt-fix/docs/HANDOFF.md` and `app/docs/HANDOFF.md` are the same
  note, `app/docs/HANDOFF.md`.
- Outside a repository, identity is the file's own path.
- Under a root marked `newest_only`, identity is the root joined to the file name, so
  every `HANDOFF.md` anywhere below that root is one note.

The record is built from the most recently written copy. Its `ref` is the identity
path, which is stable while the winning copy changes, so an edit in any worktree
updates the same record in place through the existing `record_map` path (same id,
same vault file). Nothing new is needed in the pipeline to avoid duplicates.

**When a copy was written.** For a tracked file whose working copy is exactly what
`HEAD` holds (its git blob hash equals `HEAD:<path>`), the time is that of the last
commit that touched it. For anything else (untracked, edited since the commit,
outside a repository, git not installed or not answering within 10 seconds) it is
the file's modification time. Git is asked through two read-only commands
(`rev-parse`, `log -1`) with `GIT_OPTIONAL_LOCKS=0`, so it never takes the index
lock in a tree another session is working in, and once per version of a file: the
answer is cached against the file's path, size and modification time.

**Project and date.** The project is the main working tree's directory name; outside
a repository it is the first directory below the root. A note with no directory of
its own below the root (it sits in the root itself, or the root is `newest_only`)
takes the name of the repository that contains the root, else the root's name. A
repository above the root is used only for that name, never to group copies, so a
home directory that is a dotfiles repository does not turn every project into one.
The date is the date in the file name when there is one (`2026-11-13`, `20261113`),
else the local date the winning copy was written. Both go where each search leg can
see them:

- title: `<project> handoff <date>: <frontmatter title, first heading, or file stem>`
  (keyword search and the Jev reranker read titles);
- tags: `handoff` and `project:<project>`, after any tags in the note's frontmatter;
- one line at the top of the body naming the project, the date and the identity path
  (vectors are built from the body only, so without it a semantic query that names
  the project has nothing to match).

`created` is the file-name date when present, else the time the copy was written;
`updated` is the time the copy was written.

**Polling.** `watch()` rescans every `poll_seconds` (default 300) and emits a note
only when its winning copy's path, size or modification time changed since the last
emit. `reconcile()` emits every note; the daemon's skip-unchanged check (ADR 0024)
drops those whose body is already stored. The walk and the file reads run in a
worker thread.

**Deletion.** Records are type `memory`, so ADR 0026 applies unchanged. The adapter
answers `source_exists(ref)` from its last scan (a note exists while any copy of it
does), because the identity path need not exist on disk: the newest copy may live in
a worktree, and a `newest_only` identity is never a real file. A note whose every
copy is gone is tombstoned by the existing sweep, with its root-liveness guard and
per-root valve.

**Opt-in.** Off by default with no default roots. A vault ingests project notes only
when its operator names the directories.

## Consequences

- A handoff note's decisions and open items are searchable as written, under a title
  that names the project and the date.
- When two branches keep genuinely different notes at the same path, only the newer
  is searchable. That is the intended trade: the common case by far is one note at
  different points in time.
- A rolling note has one record that always shows its latest text. Earlier text
  survives in the sessions that wrote it, not in the note's record.
- The identity path shown as `ref` may not exist in the main working tree when the
  note has only ever been written in a worktree.
- If the main working tree lies outside every configured root, the sweep cannot name
  an owning root for the note and falls back to its containing-directory check.
- Do not point a root at a directory another adapter already ingests (an OpenClaw
  workspace's `memory/`): the header line makes the two bodies differ, so exact-body
  dedup (ADR 0012) would not collapse them.
- Changes are picked up within `poll_seconds`, not within the watchdog debounce.
- The first scan after a daemon start runs two git commands per copy found inside a
  repository (6 seconds for 300 copies on the measured host, in a worker thread);
  later scans run them only for files that changed.

## Alternatives considered

- **`extra_files` on an existing adapter.** A fixed list goes stale the day a new
  note is written, and gives no project, date or worktree handling.
- **A recursive watchdog watch on project roots.** One inotify watch per directory
  across 108,000 directories, most of them dependency trees.
- **Index every copy and let exact-body dedup (ADR 0012) collapse them.** It
  collapses identical copies only. The cases that matter are an edited note beside
  its stale copies.
- **Summarise notes with an LLM into decision records.** Deferred: the note is
  already the author's summary, and the measured gap is that it is not indexed.
