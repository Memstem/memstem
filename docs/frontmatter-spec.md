# Frontmatter Specification

Every file in the Memstem vault begins with a YAML frontmatter block. This is the schema.

## Required fields (all memory types)

```yaml
---
id: <uuid-v7>             # globally unique, time-ordered
type: <memory_type>       # see types below
created: <iso8601>        # creation timestamp
updated: <iso8601>        # last modification timestamp
source: <adapter_name>    # which adapter ingested this (or 'human')
---
```

## Memory types

- `memory` — generic fact, decision, observation
- `skill` — reusable procedure (authored by the consuming AI; MemStem ingests but does not author — see [ADR 0019](./decisions/0019-no-skill-authoring.md))
- `session` — chunk of a session transcript
- `daily` — date-bucketed log
- `person` — person profile
- `project` — durable rollup record for a piece of work spanning multiple sessions ([ADR 0021](./decisions/0021-project-records.md))
- `decision` — decision record
- `distillation` — derived summary record produced by the hygiene worker ([ADR 0008](./decisions/0008-tiered-memory.md), [ADR 0020](./decisions/0020-session-distillation-writer.md)). Always carries `links` back to source records. The session-distillation writer produces one per meaningful session; future topic-distillation work will add cluster rollups under the same type.

## Optional fields

```yaml
title: <string>           # human-readable title
tags: [<string>, ...]     # taxonomy tags
links: [<wikilink>, ...]  # explicit cross-references
provenance:
  source: <adapter_name>
  ref: <opaque ref>       # e.g. claude-code session ID
  ingested_at: <iso8601>
confidence: extracted | inferred | ambiguous
importance: 0.0 - 1.0     # set by hygiene worker
valid_from: <iso8601>     # bi-temporal validity (Phase 2)
valid_to: <iso8601>       # bi-temporal validity (Phase 2)
embedding_version: <int>  # bumped when embedding model changes
deprecated_by: <id>       # supersession (Phase 2)
manual: <bool>            # project records only — `true` protects the
                          # body from regeneration by `memstem hygiene
                          # project-records` (links + updated still
                          # refresh; --force overrides). See ADR 0021.
```

## Body conventions

- **Plain markdown.** No proprietary syntax.
- **Wikilinks** `[[Entity Name]]` or `[[memory://path]]` for cross-references. The indexer extracts these as graph edges.
- **Atomic notes preferred.** One fact, one decision per file when feasible.
- **Provenance footer** (recommended for ingested content): a final line `_ingested from {source} on {date}_`

## Skill schema

Skills are memories with `type: skill` and additional required fields:

```yaml
---
id: <uuid>
type: skill
title: <string>
created: <iso8601>
updated: <iso8601>
source: <adapter_name | human>
scope: universal | <agent_name>     # which agents can use this
prerequisites: [<wikilink>, ...]
verification: <string>              # how to verify it worked
---
```

## Example: a memory

```markdown
---
id: 0192f8a7-1234-7890-abcd-ef1234567890
type: decision
title: Use Cloudflare Registrar for new domains
created: 2026-04-25T15:30:00-04:00
updated: 2026-04-25T15:30:00-04:00
source: claude-code
tags: [domains, infrastructure, cost]
provenance:
  source: claude-code
  ref: session-abc123
  ingested_at: 2026-04-25T15:35:12-04:00
confidence: extracted
---

Decided to use Cloudflare Registrar for new domain registrations because at-cost pricing saves ~$1,200/yr across 100 domains. GoDaddy renewal pricing is 2x+ higher.

Related:
- [[Cloudflare Registrar]]
- [[GoDaddy migration plan]]

_ingested from claude-code on 2026-04-25_
```

## Validation

The Memstem daemon validates frontmatter on ingest. Invalid frontmatter is rejected with a logged warning; the file remains on disk for human inspection.
