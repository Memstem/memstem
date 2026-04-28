# ADR 0012: Two-stage dedup with LLM-as-judge, invalidate-don't-delete

Date: 2026-04-27
Status: Proposed

## Context

ADR 0008 (Tier 3) sketched a one-shot dedup step: pairs with cosine
similarity ≥ 0.95 get merged, the higher-importance record wins, the
loser becomes a `deprecated_by:` redirect. That works for textually
near-identical records but fails on the harder cases that cause real
trouble in production memory systems.

The field consensus is unambiguous on three points (Graphiti, Cognee,
mem0 v3, MemPalace, mcp-memory-service):

1. **Cosine similarity is for candidate generation, never for the
   final dedup decision.** Embeddings cluster "Alice works at Acme as
   a software engineer" and "Alice works at Acme as a senior
   engineer" together — but they are *not* duplicates. They are a
   contradiction (a role-title change). A cosine threshold cannot
   tell the difference; an LLM-as-judge with explicit rules can.
2. **Hash on normalized text first, for free.** Exact duplicates from
   the hallucination feedback loop (model hallucinates fact → stores
   it → re-extracts from recall → 808 copies in mem0's audit) get
   caught with one SHA-256 lookup. There is no excuse to pay
   embedding cost on byte-identical bodies.
3. **Invalidate, don't delete.** mem0 v2 ran an UPDATE/DELETE
   classifier and reverted it in v3 because *"reconciliation was
   slow, and it was where context got destroyed... overwrites would
   erase key information from the original fact, and deletes would
   remove information that would be relevant later."* mem0 v3 is
   ADD-only and reports LongMemEval 67.8 → 93.4 from the change.

ADR 0008's frontmatter already supports `deprecated_by`, `valid_to`,
and `supersedes`. This ADR locks the *pipeline* that populates them
and replaces ADR 0008's simple cosine step.

Skills (`SKILL.md` files) require an extra gate. They are
high-leverage and used by every agent that touches MemStem; an
incorrect auto-merge of two skills causes hours of debugging when one
gets used and behaves like the other. Brad called this out
specifically: "we need just one source of truth without duplications"
applies to memories *and* skills, but skills must never be silently
merged.

This ADR depends on ADR 0011 — dedup operates on atomic facts, which
ADR 0011's extraction stage produces.

## Goals

1. Catch byte-identical duplicates at write time, free, no LLM.
2. Use embeddings for candidate generation only; do not trust cosine
   as the arbiter of identity.
3. Resolve the harder cases (near-duplicates, contradictions,
   related-but-distinct) with an LLM-as-judge whose prompt explicitly
   enumerates the failure modes the field has documented.
4. Never delete a record. Use `deprecated_by`, `valid_to`, and
   `supersedes` (already in the schema) to invalidate while keeping
   both files on disk.
5. Treat skills with extra conservatism: when the judge marks two
   skills duplicate or contradictory, queue a human review ticket;
   never auto-merge.
6. Stay 100% inside MemStem. Pure Python + local Ollama. Cross-platform.

## Non-goals

- Not building a knowledge graph. Graphiti's prompts are excellent;
  their typed-edge data model is overkill for our scale.
- Not exposing time-travel queries (`memstem search --at <date>`)
  yet. The schema fields support it; the CLI doesn't need to.
- Not auto-resolving every UNRELATED case. Records that aren't
  duplicates and aren't contradictions stand on their own; we don't
  need to add `links:` between every related pair.
- Not running on hot-write paths. Dedup runs in the hygiene worker
  (post-write), not synchronously during user queries.

## Decision

Replace ADR 0008's simple cosine step with a three-layer pipeline in
`memstem.hygiene.dedup`. Each layer is independently revertible.

### Layer 1 — Exact-hash dedup (write-time, no LLM)

On every write through `core/pipeline.upsert()`:

1. Whitespace-normalize the body: collapse runs of whitespace to a
   single space, trim, lowercase.
2. SHA-256 the normalized body → `body_hash`.
3. Look up `body_hash` in a new `body_hash_index` table in
   `_meta/index.db`. If present, increment a `seen_count` counter on
   the existing record and skip the new write.
4. Otherwise, insert the new row and proceed to Layer 2.

This catches the hallucination feedback loop for free. mem0's 808-copy
case would have been a single record with `seen_count: 808`.

### Layer 2 — Embedding candidate generation

For every record that passes Layer 1:

1. Compute / fetch the embedding (already done by `embed_worker`).
2. `vec_search` for top-5 nearest neighbors with cosine ≥ 0.85.
3. Filter out neighbors whose `body_hash` matches the new record's
   (already handled by Layer 1; defense in depth).
4. If no candidates: write as a fresh record. Done.
5. Else: pass `(new, candidates)` to Layer 3.

The 0.85 threshold is intentionally permissive — we want the judge to
see anything plausibly related. False positives at this layer are
free; the judge filters them.

### Layer 3 — LLM-as-judge

For each `(new, candidates)` batch, call the local LLM (Ollama,
`qwen2.5:7b` default — the same model ADR 0011 uses for extraction)
with the dedup-judge prompt. Output is a JSON list of verdicts, one
per candidate.

### Layer 4 (skills only) — Human review queue

When the judge returns `DUPLICATE` or `CONTRADICTS` for any pair where
*either side* is `type: skill`:

1. Do **not** auto-apply the verdict.
2. Write a review ticket to
   `vault/skills/_review/<utc_iso>-<slug>.md` containing both
   candidates, the verdict, and the judge's rationale.
3. The new skill record is written normally (no `deprecated_by`); the
   conflict is queued.
4. `memstem skill-review` CLI lists open tickets; `memstem
   skill-review apply <ticket>` and `memstem skill-review dismiss
   <ticket>` resolve them.

This is the only divergence between memory and skill handling. For
non-skill records, the judge's verdict is applied automatically.

## The dedup-judge prompt

Stored at `src/memstem/prompts/dedup_judge.txt`. Adapted from
Graphiti's `dedupe_edges.py` and `dedupe_nodes.py` — both MIT-licensed
and battle-tested in the Zep production system. Ported almost
verbatim, with terminology changed from "edges/entities" to "facts" to
match our flat-record model.

```
You are a deduplication judge for a memory system. Given a NEW fact
and a list of EXISTING facts that may be related, classify each
(NEW, EXISTING) pair into exactly one category.

Categories:
- DUPLICATE: The same fact. Same entities, same relationship, same
  qualifiers, same numeric values, same dates. The two records say
  the literal same thing.
- CONTRADICTS: Same entities and relationship type, but the value or
  qualifier differs. A role-title change, a status change, a numeric
  update — these are contradictions, NOT duplicates. The newer fact
  invalidates the older one.
- RELATED_BUT_DISTINCT: The records share entities but describe
  different relationships, or describe the same entities at different
  scopes. Both should stand independently.
- UNRELATED: No meaningful overlap.

RULES — read carefully, the field has documented these failure modes:
- NEVER mark facts as duplicates if they have key differences,
  particularly around numeric values, dates, or key qualifiers.
- A change in title, status, count, or quantity is CONTRADICTS, not
  DUPLICATE. Example: "Alice is a software engineer at Acme" vs.
  "Alice is a senior engineer at Acme" → CONTRADICTS.
- Same name, different real-world referent → UNRELATED. Example:
  "Java" (programming language) vs. "Java" (Indonesian island).
- Reformulations of the same fact (passive vs. active voice, synonym
  swap, abbreviation expansion) are DUPLICATE. Example: "NYC has 8M
  people" vs. "New York City's population is 8 million" → DUPLICATE.
- When unsure, prefer RELATED_BUT_DISTINCT over DUPLICATE. False
  duplicates destroy information; false distincts are recoverable.

NEW FACT:
id: {new_id}
body: {new_body}

EXISTING FACTS:
{candidates_json}

Return strictly valid JSON of the form:
[
  {"existing_id": "<id>", "verdict": "DUPLICATE|CONTRADICTS|RELATED_BUT_DISTINCT|UNRELATED", "rationale": "<one sentence>"}
]
```

The "When unsure, prefer RELATED_BUT_DISTINCT" rule is our addition
— Graphiti doesn't have it, but it directly maps mem0's v2→v3 lesson
into prompt form: false duplicates destroy information.

## Resolution rules

The judge returns a list of verdicts. For each:

| Verdict                  | Action                                                                                                                             |
|--------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| `DUPLICATE` (memory)     | New record gains `deprecated_by: <existing_id>`. Existing wins. Both files stay on disk; default search returns existing.         |
| `DUPLICATE` (skill)      | Write review ticket. Both records stay normal. No auto-apply.                                                                     |
| `CONTRADICTS` (memory)   | Existing gains `valid_to: <now>`. New gains `supersedes: [<existing_id>]`. Default search returns new; `--include-historical` returns both. |
| `CONTRADICTS` (skill)    | Write review ticket. Both records stay normal. No auto-apply.                                                                     |
| `RELATED_BUT_DISTINCT`   | New record gains `links: [<existing_id>, ...]`. Both stand independently. Bidirectional link is opt-in via `memstem link-back`.   |
| `UNRELATED`              | New record stands. No frontmatter changes.                                                                                        |

All resolutions are reversible: every action is a frontmatter edit on
canonical markdown. Drop the index, run `memstem reindex`, the state
is recovered.

## Pseudocode

```python
def dedup_pipeline(new: MemoryRecord) -> list[Action]:
    h = body_hash(new)
    if hash_index.contains(h):
        hash_index.increment_seen_count(h)
        return [Action.SKIP_HASH_DUPLICATE]

    candidates = vec_search(new.embedding, k=5, min_cosine=0.85)
    candidates = [c for c in candidates if body_hash(c) != h]
    if not candidates:
        hash_index.insert(h, new.id)
        return [Action.WRITE_NEW]

    verdicts = llm_judge(new, candidates)
    actions = []
    for v in verdicts:
        if new.type == "skill" or v.existing.type == "skill":
            if v.verdict in ("DUPLICATE", "CONTRADICTS"):
                actions.append(write_review_ticket(new, v))
                continue
        actions.append(resolve_memory_verdict(new, v))
    return actions
```

## Schema additions

ADR 0008 already added `deprecated_by`, `valid_to`, `supersedes`, and
`links`. This ADR adds nothing to user-visible frontmatter.

Internal `_meta/index.db` gains:

| Table              | Columns                                       | Purpose                                                       |
|--------------------|-----------------------------------------------|---------------------------------------------------------------|
| `body_hash_index`  | `body_hash` PK, `memory_id`, `seen_count`, `last_seen` | Layer 1 lookup. Rebuildable from frontmatter on `reindex`. |
| `dedup_audit`      | `ts`, `new_id`, `existing_id`, `verdict`, `rationale`, `applied` | Append-only audit log of every judge decision. |

`dedup_audit` is non-canonical (we accept losing it on crash; backups
are trivial — single SQLite file).

## Implementation phasing

1. **PR-A: Layer 1 only.** Add `body_hash_index`, integrate into
   pipeline upsert. Cheapest possible win; reverts cleanly. ~150 LOC.
2. **PR-B: Layer 2 candidate query.** Reuse existing vec_search
   plumbing. Pure read path. ~100 LOC.
   *Status: shipped (Unreleased, 2026-04-28).
   `memstem hygiene dedup-candidates` walks the vector index, finds
   memory pairs whose first-chunk embeddings cross a cosine
   threshold (default 0.85), and reports them. Read-only —
   no mutations. Skill-vs-anything pairs are flagged for the
   operator to be cautious during manual review until Layer 3
   lands.*
3. **PR-C: Layer 3 judge + audit log.** Includes prompt loader,
   Ollama call, JSON parser, audit writes. ~300 LOC.
4. **PR-D: Resolution actions for memories.** `deprecated_by`,
   `valid_to`, `supersedes`, `links` writers. ~250 LOC.
5. **PR-E: Skill review queue + CLI.** `_review/` directory writer,
   `memstem skill-review` subcommand. ~200 LOC.

PR-A can ship within days. PR-C is the LLM-prompt-tuning critical
path; budget two weeks including the eval suite.

## Rationale

- **Hash first because it's free.** The body-hash check is a single
  SQLite indexed lookup. It catches the worst case (feedback-loop
  duplicates) before any cost is paid. Skipping this layer is
  malpractice once you know it exists.
- **Cosine for candidates because the field is unanimous.** Graphiti,
  Cognee, mem0 v3, mcp-memory-service all converged on
  candidate-generation-from-embeddings, decision-from-LLM. Five
  independent teams ran the experiment; we don't need to repeat it.
- **LLM judge because the failure modes are documented.** Graphiti's
  prompt explicitly enumerates "NEVER mark X as duplicate if Y" rules
  with worked examples. Their prompts work in production at Zep
  (commercial offering). Copy verbatim, port the terminology,
  done — there is no reason to invent new prompts here.
- **Invalidate-don't-delete because mem0 paid the price.** The mem0
  v2 → v3 reversal is the single most expensive lesson in this
  field's public record. We refuse to repeat it. Every decision keeps
  both records on disk.
- **Skills get a human gate because the cost asymmetry is large.**
  Auto-merging a skill that turns out to be distinct burns hours of
  agent debugging across every workflow that uses it. The cost of
  manual review (one CLI command per ticket) is much lower than the
  cost of one bad merge.
- **Local Ollama because cross-platform and self-contained.** Same
  reasoning as ADR 0011. The judge call adds 1-3 seconds per
  candidate batch; runs in the hygiene worker, never on a hot path.

## Consequences

**Pros:**

- Solves dedup for both memories and skills with one pipeline.
- Catches the hallucination feedback loop with one SHA-256 lookup
  per write — a virtually-free guard against the worst failure mode.
- Uses prompts that already work in production at another vendor.
- 100% MemStem-internal; no Claude Code or OpenClaw surface area.
- Cross-platform (Python + Ollama + SQLite — all run identically on
  Linux, macOS, Windows).
- Every decision is reversible because every action is a
  frontmatter edit on canonical markdown.

**Cons:**

- LLM judge adds 1-3 seconds per candidate batch in the hygiene
  worker. Mitigation: batches multiple candidates in one call;
  worker is async; UI never waits.
- Judge can be wrong in edge cases (false DUPLICATE on a CONTRADICTS
  pair would lose information). Mitigation: the "prefer
  RELATED_BUT_DISTINCT when unsure" rule + audit log + the fact that
  no record is ever physically deleted.
- Skill review queue requires user attention. Mitigation: this is
  intentional; auto-merging skills is the failure mode we are
  preventing.
- The `body_hash_index` table grows with the corpus. Mitigation: row
  size is ~80 bytes; even 1M records is 80MB.

## Open questions

- Should the judge call be batched across multiple `(new, candidates)`
  pairs in one prompt for throughput? Decide in PR-C with a real
  benchmark on Brad's box.
- For very long bodies (>16KB), do we hash on the full body or the
  first 4KB? Current draft: full body — collisions are not a real
  risk and partial-hash false negatives reintroduce the feedback
  loop.
- Do we need `memstem dedup undo <audit_id>` to reverse a judge
  decision? Probably yes for the first 30 days post-launch.
- Should `RELATED_BUT_DISTINCT` always create a `links:` entry, or
  only on user request? Current draft: yes by default — links are
  cheap and aid navigation.

## References

- ADR 0008 (Tier 3 dedup — this ADR supersedes its simple cosine step)
- ADR 0011 (atomic-fact extraction — produces the records this dedups)
- mem0 v2→v3 reversal: https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm
- mem0 audit failure modes: https://github.com/mem0ai/mem0/issues/4573
- Graphiti dedupe_edges.py: https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/dedupe_edges.py
- Graphiti dedupe_nodes.py: https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/dedupe_nodes.py
- Zep arXiv paper: https://arxiv.org/html/2501.13956v1
