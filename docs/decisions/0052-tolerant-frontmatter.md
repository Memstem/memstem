# 0052 — Read agent-authored frontmatter leniently instead of skipping the file

Status: **Accepted**
Date: 2026-10-11
Related: ADR 0005 (pull-based ingestion), PR #190 (skipped-file visibility, 0.24.0)

## Context

Adapters parse each source markdown file with python-frontmatter, which hands the
header to PyYAML. When PyYAML rejects the header the adapter logs a warning, records the
path in the skipped-files registry (0.24.0) and drops the file. The file's *content* is
then absent from search over a punctuation mark in its header.

The failure is not rare and it is not random. Agents write memory notes with a one-line
prose `description:` (Claude Code's auto-memory format has exactly this field), and
prose contains `: ` — `Browser support: Chrome 114+`, `(Skyler: "All 5")`, `Skyler: lead
designer`. YAML reads the unquoted value as a nested mapping and fails with `mapping
values are not allowed in this context`. One fleet host (E1) produced seven daily health
warnings for this between 2026-09-19 and 2026-10-11, ten files in all, each repaired by
hand while new ones kept arriving; a client workstation lost a file to it for sixteen
days in September. Other hosts are unaffected only because their agents' memory files
carry no frontmatter at all, or sit in directories MemStem does not scan.

ADR 0005 and `frontmatter.coerce()` already commit MemStem to ingesting whatever an agent
natively writes: a memory's content must never be lost to a metadata problem. Skipping a
file over a colon in its description contradicts that contract.

## Decision

`memstem.core.frontmatter.load_post()` is the one parser for agent-authored markdown.
It tries strict YAML first; a clean file is handled exactly as before. When YAML fails
it reads the header one `key: value` line at a time:

- a value that YAML accepts on its own keeps its YAML type (dates, numbers, booleans,
  quoted strings, flow lists), so a recovered file matches a clean one;
- a value YAML rejects, or would turn into a *mapping*, is kept as the literal string
  — which is what the author meant;
- a bare `key:` takes the indented block beneath it as a list (`- item`) or a nested
  mapping, recursively;
- a line that is not `key: value` continues the previous string value (YAML's own rule
  for wrapped plain scalars).

A header whose first non-blank line is not `key: value`, that has no keys, or that lacks
a closing `---` is still unparseable: the original YAML error is re-raised and the file
is reported as before (`/health` `skipped_files`, `memstem doctor`). The lenient reader
never invents metadata for something that is not a header.

The three adapters and the doctor scan call `load_post()`. A lenient parse is logged at
INFO (`frontmatter read leniently for <path> (not valid YAML: …)`) so the header can be
tidied; it is not a `/health` problem, because the file is in the index.

## Consequences

- Content is never lost over a header punctuation error; the class of incident above
  ends fleet-wide once deployed.
- Recovered metadata for the offending key is a string where YAML would have produced a
  mapping. Downstream code already treats `title`/`description`/`name` as strings; other
  keys keep YAML types whenever the value parses alone.
- `skipped_files` now means "not a frontmatter header at all", a narrower and more
  actionable signal.
- Possible follow-up, not done here: a `recovered_files` count on `/health` or in
  `doctor`, if operators want a nudge to fix headers that only parse leniently.
