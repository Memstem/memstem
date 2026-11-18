# Handoff notes

Agents end long work by writing a handoff note into the project: `HANDOFF.md`,
`HANDOFF-2026-11-13-new-tab.md`, `docs/pre-handoff-gate3.md`. Memstem can keep one
searchable record per note, titled with the project and the note's date. Design and
reasoning: [ADR 0057](./decisions/0057-handoff-notes.md).

It is off by default. A vault reads project directories only when you name them.

## Turn it on

In `<vault>/_meta/config.yaml`:

```yaml
adapters:
  handoff:
    enabled: true
    roots:
      - ~/projects
      - ~/work/clients
```

Restart the daemon. The first scan runs in the background a few seconds after start;
after that the roots are rescanned every five minutes.

## What becomes a record

A file below a root whose name matches `*handoff*.md` (case does not matter) and does
not match `*template*`, up to 2 MB. Hidden directories (`.git`, `.claude`, `.venv`),
`node_modules`, `site-packages`, `venv`, `__pycache__`, `dist`, `build`, `target` and
`vendor` are not entered, and nothing deeper than 8 directories below the root is read.

Each note becomes a `memory` record:

| Field | Value |
|---|---|
| title | `<project> handoff <date>: <the note's title or first heading>` |
| tags | `handoff`, `project:<project>`, plus any tags in the note's frontmatter |
| body | one line naming the project, date and path, then the note |
| source | `handoff` |

The **project** is the repository's directory name (for a git worktree, the main
repository's), or the first directory below the root when the note is not in a
repository. The **date** is the date in the file name if it has one, else the day the
note was last written.

## One record per note

Git worktrees hold a copy of every tracked note. Memstem treats
`app-wt-fix/docs/HANDOFF.md` and `app/docs/HANDOFF.md` as the same note and indexes the
most recently written copy. Edit the note in any worktree and the same record is
updated within five minutes; the older text is no longer returned.

"Most recently written" is the commit time for a file that matches what is committed,
and the file's modification time for one that has been edited since or is not tracked.
A new worktree of an old branch therefore does not displace a note edited yesterday.

A note is removed from search when its last copy is deleted.

## Rolling notes written to a new directory each time

If a project writes a fresh copy of its long-running note into a new directory every
session (`runs/2026-11-13/HANDOFF.md`, `runs/2026-11-14/HANDOFF.md`), mark that
directory `newest_only`. Every file of the same name below it is then one note, and
only the newest is indexed:

```yaml
    roots:
      - ~/projects
      - path: ~/projects/shop/.private
        newest_only: true
```

A root is always read even when it is hidden or sits inside a hidden directory, as
`.private` does here: hidden directories are skipped only when found below a root.

## Options

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn ingestion on. |
| `roots` | `[]` | Directories to scan: a path, or `path` with `newest_only: true`. |
| `patterns` | `["*handoff*.md"]` | File-name globs that mark a note. |
| `exclude_patterns` | `["*template*"]` | File-name globs to leave out. |
| `exclude_dirs` | `[]` | Extra directory names to skip, for example `backups` or `archive`. |
| `max_depth` | `8` | How many directories below a root to read. |
| `max_file_bytes` | `2097152` | Larger files are left out. |
| `poll_seconds` | `300` | Time between scans. |

Do not point a root at a directory another adapter already ingests, such as an
OpenClaw workspace's `memory/`: the same file would be stored twice.
