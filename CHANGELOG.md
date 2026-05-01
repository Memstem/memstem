# Changelog

All notable changes to Memstem will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] — 2026-05-01

The "derived records" release. Five PRs (#90–#94) ship Block 4 of
RECALL-PLAN.md: session distillation writer (W8, ADR 0020) and
project records (W9, ADR 0021). Direct fix for the recall failure
where natural-language queries like "the Woodfield Country Club
e-bike video" or "the project where we revised the aerial demo"
fail to surface project work that exists in the vault — the data
is there, the *shape* is wrong. Both new commands ship CLI-driven
and idempotent; the recommended workflow is one backfill pass per
command at cutover, then routine runs from cron / PM2 / `memstem
schedule`.

ADR 0019's "no skill authoring" rule stands unchanged — distillations
and project records are search-shape derivatives with mandatory
provenance back to the source, not skill-style standalone knowledge
claims. The boundary table in ADRs 0020/0021 spells out the
distinction.

Schema migration: 10 → 11 (`summarizer_cache` table — shared by
both writers; non-canonical, drop-and-rebuild safe).

### Added

- **Session distillation writer (ADR 0020 / W8).** `memstem hygiene
  distill-sessions [--apply] [--backfill] [--force]` walks the vault,
  applies a meaningfulness threshold (≥10 turns + ≥100 words),
  skips already-distilled sessions, and produces a 1-paragraph
  rollup with structured Key entities / Deliverables / Decisions /
  Status sections at `vault/distillations/<source>/<session_id>.md`.
  Each distillation links back to its source session via `links` and
  carries `provenance.ref = session-distillation:<id>`. Importance
  seed = 0.8 so the existing `alpha=0.2` multiplier surfaces
  distillations above raw transcripts on close ties without
  bulldozing skills/decisions. `--force` regenerates against the
  prompt template; the existing memory_id is preserved for in-place
  overwrite. NoOp summarizer is the safe default; opt into OpenAI
  (`gpt-5.4-mini`) or Ollama (`qwen2.5:7b`) explicitly via
  `--provider`.
- **Project records writer (ADR 0021 / W9).** `memstem hygiene
  project-records [--apply] [--force]` aggregates Claude Code
  sessions sharing a project tag (e.g.
  `home-ubuntu-woodfield-quotes`) into a single `type: project`
  record at `vault/memories/projects/<slug>.md` — canonical project
  name extracted from the work itself, description, participants,
  deliverables, accumulated decisions, latest known state. Reuses
  the W8 summarizer abstraction with a separate prompt template;
  prefers session distillations over raw bodies when both exist.
  Threshold: ≥2 sessions per project tag. `manual: true` in the
  record's frontmatter preserves hand-curated bodies on re-run
  (only `links` and `updated` refresh); `--force` overrides.
  Importance seed = 0.85, just above session distillations (0.8),
  so a project record outranks any specific session for the same
  project on close ties.
- **Generic summarizer abstraction (PR #91).** New
  `core/summarizer.py` mirrors the rerank/HyDE/dedup-judge pattern:
  `Summarizer` ABC + `NoOpSummarizer` + `StubSummarizer` +
  `OllamaSummarizer` + `OpenAISummarizer`, lazy httpx, content-keyed
  cache. Default OpenAI model is `gpt-5.4-mini` because summary
  text IS the search target — quality matters more than for
  rerank/HyDE where the LLM output is a score or query rewrite.
- **Schema migration v11.** `summarizer_cache(content_hash,
  summarizer, output, ts)`. Shared by W8 and W9; cache-isolation
  via the `summarizer` column so swapping models doesn't serve
  stale outputs. Non-canonical, drop-and-rebuild safe.
- **Updated `docs/recall-models.md`** with session distillation +
  project records rows in the TL;DR, a "why `gpt-5.4-mini` not
  `gpt-4o-mini`" callout, per-feature upgrade ladder, refreshed
  cost expectations, and an Ollama-on-CPU operator note (summary
  generation is heavier than rerank/HyDE on local hardware).
- **New `docs/distillation-verification.md`** — operator playbook
  walking through the post-cutover flow: NoOp dry-run → real-provider
  dry-run → apply → manual quality spot-check → eval harness diff →
  routine maintenance.
- **Eval queries.** Four `project_*` queries in `eval/queries.yaml`
  exercise the recall failure mode that motivated W8/W9. They report
  baseline today and should land on shape-optimized records after
  the first apply pass — the lift is the gate for any default-on
  flip.
- **ADRs 0020 + 0021** documenting the design + the boundary against
  ADR 0019's "no skill authoring" rule.

### Changed

- **Default OpenAI summarization model is `gpt-5.4-mini`.** Same
  caveat as the W5/W6 defaults in 0.8.1: NoOp is the install-time
  default, the user opts into a real provider explicitly. The eval
  harness gates any future default-on flip.

### Notes

- 1087 tests pass on Linux 3.11/3.12 (114 new in PRs #91–#93).
  Cross-platform CI matrix unchanged: macOS + Windows continue-on-
  error.
- Brad's Ari vault should not see any of this until the upgrade pass
  documented in `docs/distillation-verification.md` runs explicitly.
- Cost at typical MemStem volumes for a one-shot backfill of ~356
  Claude Code sessions on `gpt-5.4-mini`: ~$5 one time, ~$3-4/month
  steady-state. NoOp + Ollama path is $0/month + the hardware you
  already have.

## [0.8.1] — 2026-04-30

The "recall quality + macOS unblocked" release. Five PRs (#84–#88)
ship the cross-encoder rerank and HyDE scaffolding from
RECALL-PLAN.md Block 2 plus a multi-provider chat-model story
(OpenAI alongside Ollama, with a recommended-models guide). One
follow-up PR (#89) closes the "macOS users hit a wall" gap that
showed up on the first day public — `install.sh` now detects
SQLite-extension-disabled Pythons up front and tells users exactly
how to fix it (Homebrew or pyenv) instead of letting them crash
with an `enable_load_extension` AttributeError later. ADR 0019
documents the architectural decision that MemStem does not author
skills — each AI's skill-generator stays in its own repo; MemStem
indexes the resulting `SKILL.md` files.

Schema migrations: 8 → 9 → 10 (rerank_cache table for ADR 0017,
hyde_cache table for ADR 0018; both are non-canonical, drop-and-
rebuild safe).

### Added

- **macOS install path with up-front detection.** `scripts/install.sh`
  now checks for SQLite extension-loading capability after the Python
  version check and bails with an actionable error if missing.
  macOS's system Python (`/usr/bin/python3`) ships with a SQLite
  built without `enable_load_extension`, which would otherwise crash
  Memstem at first index open with a confusing `AttributeError`.
  The error message names the detected Python and version, then
  walks the user through the fix — `brew install python@3.12` or
  `pyenv install 3.12.5` — and tells them to re-run the installer.
  New "macOS install" section in the README documents the same
  thing for users who prefer reading docs first. CI is unaffected
  (Linux gating + macOS continue-on-error). Closes the day-one
  install gap for the ~half of cloners on Mac.

### Changed

- **MemStem does not author skills (ADR 0019).** Removed
  auto-skill-extraction and reflective-synthesis from the roadmap.
  Each AI (Claude Code, Codex, Hermes, OpenClaw, …) generates skills
  its own way; MemStem ingests `SKILL.md` files from disk and
  indexes them, but doesn't author its own. Authoring would couple
  MemStem to a specific skill format and add a runtime LLM
  dependency to the daily hygiene loop — exactly the per-AI
  coupling the read-files-from-disk architecture was designed to
  avoid. Affects: `PLAN.md` (auto-skill bullet removed),
  `ARCHITECTURE.md` (hygiene-worker skill-extraction bullet
  removed), ADR 0008 (PR-G dropped, capability bullet annotated),
  `RECALL-PLAN.md` (W7 reflective-synthesis dropped from buckets,
  ordering, dependencies, deferred-list, and risks). New ADR 0019
  documents the decision and the boundary rule (mutating existing
  records is in scope; LLM-authoring new records is out).

### Fixed

- **Rerank API 400 errors on oversized memory bodies.** Two
  multi-megabyte ``Infrastructure — Extended Context`` memories in
  Brad's vault (1.7 MB and 1.5 MB bodies) blew past every chat
  model's context window, causing every rerank score on those
  candidates to fail with HTTP 400 from OpenAI (and silent context
  overflow with Ollama). Failed candidates returned 0.0 and fell
  to the bottom of the rerank order — silently corrupting the
  ranking. Fixed by truncating the document body in the rerank
  prompt to ``MAX_RERANK_BODY_CHARS = 4000`` (plenty for relevance
  judgment, fits in any provider's context). The truncated slice
  carries a ``[…document continues for N more chars]`` marker so
  the LLM knows it's looking at a head sample. Cache key still
  uses the full-body hash, so cache invalidation on body edits
  works correctly. Surfaced during the first end-to-end eval run
  (see ``docs/recall-eval-results.md``).

### Added

- **First end-to-end W5/W6 eval results** at
  ``docs/recall-eval-results.md``. Both features fail their default-on
  gates on Brad's vault as configured (W5 -12% aggregate MRR vs
  ≥15% required; W6 -10% on procedural class vs ≥20% required).
  Per-class breakdown surfaces asymmetric effects: W5 helps
  conceptual (+25%) but breaks historical (-44%); W6 helps factual
  (+6%) but breaks procedural (-10%) — the classic HyDE-on-domain-
  specific-corpus failure mode. R@3 and R@10 are largely unaffected.
  Eval gate working as designed; defaults stay off. Document
  includes follow-up tuning ideas for any future PR.
- **OpenAI provider for cross-encoder rerank and HyDE query
  expansion.** New `OpenAIReranker` (in `core/rerank.py`) and
  `OpenAIExpander` (in `core/hyde.py`) talk to
  `{base_url}/chat/completions` with the standard OpenAI shape.
  API key via the existing `memstem.auth` (env var or
  `~/.config/memstem/secrets.yaml`); the same Bearer-token pattern
  the existing `OpenAIEmbedder` uses. `base_url` is configurable
  for any OpenAI-compatible endpoint (Together, LM Studio, vLLM,
  …). Default model `gpt-4o-mini` for both. Cache rows are isolated
  per-judge so swapping providers doesn't serve stale scores from
  the other one. New `docs/recall-models.md` documents the
  recommended model for each feature plus an upgrade ladder
  (`gpt-4o-mini` → `gpt-4.1-mini` → `gpt-4o` → `gpt-4.1`) for
  results that don't meet the eval bar. Closes the
  no-OpenAI-option gap on the chat-model side; the existing
  Ollama variants stay default for local-only setups.
- **HyDE query expansion scaffolding (ADR 0018, RECALL-PLAN.md W6).**
  New `core/hyde.py` ships a `HydeExpander` ABC plus three
  implementations (`NoOpExpander` default-fallback, `StubExpander`
  for tests, `OllamaExpander` for production). When `Search.search`
  is called with `use_hyde=True`, the expander rewrites the query
  into a hypothetical-answer passage and that passage gets embedded
  as the vec query. BM25 still uses the original query — HyDE
  replaces semantic-space proximity, not lexical match. A
  `should_expand` gate filters out short queries, quoted strings,
  boolean operators, and identifier shapes (UUIDs, hex hashes, file
  paths) so HyDE doesn't burn LLM cycles on exact lookups. New
  SQLite migration v10 adds `hyde_cache(query_hash, judge,
  hypothesis, ts)` so repeat queries skip the LLM round trip;
  `judge` is part of the key so swapping models invalidates the
  right rows. Empty hypotheses (LLM failure) are NOT cached —
  caching failure would lock it in until manual cache clear. Prompt
  template at `prompts/hyde.txt` asks for a one-paragraph
  passage (~80-150 words) using the vocabulary the answer would
  use. Default-off; flipping the default to on is gated on the
  follow-up PR demonstrating ≥20% MRR lift on procedural-class
  queries with no factual-class regression (per ADR 0015).
  Default-off eval matches the prior baseline exactly.
- **Cross-encoder rerank scaffolding (ADR 0017, RECALL-PLAN.md W5).**
  New `core/rerank.py` ships a `Reranker` ABC plus three
  implementations (`NoOpReranker` default-fallback, `StubReranker`
  for tests, `OllamaReranker` for production). `Search.search` grows
  an optional `rerank_top_n` parameter that re-scores the top-N
  materialized candidates via the configured reranker, sorted by
  rerank score with RRF as the tiebreaker. The stage runs after RRF
  + importance and before MMR so MMR diversifies a precision-ordered
  pool. New SQLite migration v9 adds `rerank_cache` keyed on
  `(query_hash, memory_id, body_hash, judge)` so repeat scores against
  unchanged content skip the LLM round trip; `judge` is part of the
  key so swapping reranker variants doesn't serve stale scores.
  Prompt template at `prompts/rerank.txt` asks the LLM for an integer
  in [0, 100] (more reliable than float), normalized to [0, 1] and
  clamped. Default-off; flipping the default to on is gated on a
  follow-up PR with eval data showing ≥15% MRR lift (per ADR 0015).
  Default-off eval matches the previous baseline exactly: MRR 0.737,
  R@3 0.750, R@10 0.917, 11/12 found.

### Fixed

- **Embed worker no longer crashes when the parent memory is deleted
  mid-embed.** The race: the worker pops a `memory_id` off
  `embed_queue`, takes seconds to round-trip the embedder, and during
  that window the pipeline can delete the parent (path displacement
  during reconcile, or an explicit removal of the source file). When
  the worker came back and called `record_embed_state`, the FK on
  `embed_state.memory_id` rejected the INSERT with
  `sqlite3.IntegrityError: FOREIGN KEY constraint failed`, crashing
  the worker iteration. Surfaced on the busiest vault (Ari, 1.3 GB
  index) during the v0.7 → v0.8 embedder migration; never observed
  on smaller vaults (Ultra, 79 MB) where the race window almost
  never opens. `Index.record_embed_state` now treats the FK
  violation as a normal outcome — the cascade has already cleaned
  `embed_queue` / `embed_state`, so there's nothing to record — and
  also cleans any orphan `memories_vec` rows the worker may have
  written for the now-deleted parent (vec0 doesn't enforce FK and
  would otherwise leak orphans until the next reconcile pass that
  touches the same path). Worker advances cleanly. Logged at INFO,
  not WARNING — the race is expected, not an error.

## [0.8.0] — 2026-04-29

The "operability" release. Seven PRs merged off `main` (#68–#76) that
together close two operability gaps: the CLI hung on every shell
invocation against a populated index, and OpenClaw workspaces with
foundational system files beyond `MEMORY.md` / `CLAUDE.md` had no
clean way into the vault. ADR 0014 frames the CLI work as both a bug
fix and an architectural shift — the CLI is now a thin client over
the daemon when one is reachable, falling back to direct DB only when
no daemon is running. ADR 0013 frames the workspace work as a
per-workspace `extra_files` list with end-to-end CLI visibility (init
wizard discovery, doctor checks, daemon banner).

Schema migration: 7 → 8 (one-shot `embed_state` backfill marker, runs
microseconds on already-stamped vaults).

### Fixed

- **`Index.connect()` no longer scans the full vec0 table on every
  open** (ADR 0014). The legacy `embed_state` backfill ran on every
  connect and issued a SELECT that scaled `O(memories x chunks)` —
  on a 1+ GB index that was 35 seconds of CPU per CLI invocation,
  while the long-running daemon paid the cost only once at startup.
  The backfill is now gated on the pre-migration `schema_version`
  and runs at most once per install (when crossing v8); fresh
  installs hit it via the migration loop, legacy v3..v7 installs hit
  it on first open after upgrade, and v8+ opens skip it entirely.
  `_backfill_embed_state` keeps a defensive fast-path so calling it
  on an already-stamped vault is microseconds, not seconds. Schema
  version bumped 7 → 8.

### Added

- **`memstem search` delegates to the local daemon when reachable**
  (ADR 0014). When a `memstem daemon` is running on loopback and
  serving the same vault as the CLI is configured for, the CLI now
  routes search queries through `POST /search` instead of opening
  the SQLite index in the CLI process. The daemon reuses its hot
  connection, warm embedder, and cached pages, so search returns in
  tens of milliseconds regardless of vault size. With no daemon
  running (or `--no-daemon`), the CLI opens the index directly
  exactly as before — fallback is transparent. Results are
  identical between the two paths; only latency differs. New
  module: `src/memstem/client.py` (`DaemonClient`, `find_daemon`).
- **`memstem search --no-daemon`** flag for forcing the direct-DB
  path during debugging or when troubleshooting daemon
  configuration.
- **`memstem search -v`/`--verbose`** prints structured phase
  markers to stderr — `connect`, `daemon-probe`, `daemon-search`,
  `direct-search` — each with elapsed wall-clock time. Useful for
  diagnosing slow searches without reaching for `py-spy`. Without
  `-v`, any phase that exceeds 2 seconds prints a single warning to
  stderr (`[memstem] connect took 35.3s -- set --verbose for phase
  timings`), so future regressions of the embed_state-backfill
  shape become visible in-band. New module:
  `src/memstem/progress.py` (reusable `phase()` context manager).
- **`OpenClawLayout.extra_files`** — workspace-relative top-level files
  beyond `MEMORY.md` / `CLAUDE.md` that the OpenClaw adapter ingests
  with the workspace's `agent:<tag>` tag. Closes a coverage gap on
  agents like Ari with foundational system files (`SOUL.md`,
  `USER.md`, `AGENTS.md`, `IDENTITY.md`, `TOOLS.md`, etc.) that
  previously needed to be added to `shared_files` (wrong tag) or stay
  unindexed. See ADR 0013.
- **`memstem doctor` and the daemon banner** now surface configured
  workspace `extra_files` — doctor checks each file exists; the daemon
  prints them in its startup listing for operator visibility.
- **`discover_workspace_extras()`** in `memstem.discovery` — scans a
  workspace's top-level `.md` files and returns a curated list
  suitable for `OpenClawLayout.extra_files`. Filters out files already
  handled (`MEMORY.md`, `CLAUDE.md`, `HARD-RULES.md`), dated snapshots
  (`*_FULL_*`, `INCIDENT-*`, `*-status-report-*`, `RECOVERY-*`), and
  oversize append-only logs (>50KB). The init wizard uses this to
  offer a one-prompt opt-in for each workspace's system files.
- **Once-per-machine star nudge.** After a successful `memstem init` or
  `memstem doctor`, the CLI prints a single line asking the user to star
  the repo on GitHub if memstem helps them. The same line appears at
  the end of `install.sh`. Suppressed when stdout is not a TTY (so
  scripts and CI stay clean), when `MEMSTEM_NO_NUDGE` is set in the
  env, or when `~/.config/memstem/.star-shown` already exists.
- **README badges** — stars (Shields), CI status, MIT license; star
  history chart in a new "Why star this repo" section.
- **ADR 0014: CLI daemon delegation + one-shot migration discipline**
  — locks two architectural decisions for the v0.7.x stabilization:
  (1) backfills are part of the migration step that introduces them,
  not on every connect, and (2) the CLI delegates read paths to the
  daemon when one is reachable. Lands as three sequential PRs.

## [0.7.0] — 2026-04-28

The "memory quality" release. Six PRs merged off `main` (#61–#66) that
together close the v0.x retrieval-quality loop end-to-end: importance
boosts on top of RRF, a bounded retrieval feedback log that powers
deterministic hygiene bumps, a distillation candidate report, a
near-duplicate candidate report, and an LLM-judge + audit-log
scaffold that lets a future PR write resolutions back to the vault
without surprising anyone today. None of this is destructive on
its own — every new sweep ships either dry-run or read-only by
default.

Stages shipped:

1. **Importance-aware ranking (ADR 0008 Tier 1, PR-A, #61).**
   Search applies `final = rrf * (1 + alpha * importance)` on top of
   RRF, with `alpha = search.importance_weight` (default `0.2`) and
   un-annotated memories defaulting to `importance = 0.5` so they
   aren't penalized. Tunable per-call, per-vault, and per-request.
2. **Retrieval feedback logging (ADR 0008 Tier 1, PR-B, #62).**
   Search and `memstem_get` write per-hit exposure to a bounded
   `query_log` table (schema v5). Bounded at
   `hygiene.query_log_max_rows` (default 100k); every entry point is
   wrapped in `try/except` so a corrupt log can never silently mute
   search. Default-on; disable with `hygiene.query_log_enabled = false`.
3. **Deterministic hygiene importance bumps (ADR 0008 Tier 1, PR-C,
   #63).** New `memstem hygiene importance` subcommand consumes the
   `query_log` and proposes conservative bumps (`0.05` per get,
   `0.01 / rank` per search hit, half-weighted at 30+ days, capped at
   `0.1` per sweep and `1.0` overall). Default is `--dry-run`; the
   cursor in `hygiene_state` (schema v6) only advances on `--apply`,
   so re-running is idempotent.
4. **Distillation candidate report (ADR 0008 Tier 2, PR-D first slice,
   #64).** New `memstem hygiene distill` subcommand lists clusters
   that *could* be distilled — topic-tag clusters and same-agent
   ISO-week daily-log clusters, both above `--min-cluster-size`
   (default 5). Read-only; the LLM distiller that turns a cluster
   into a `type=distillation` memory is a later PR behind an
   explicit flag.
5. **Near-duplicate candidate report (ADR 0012 Layer 2, #65).**
   New `memstem hygiene dedup-candidates` subcommand reports memory
   pairs whose first-chunk embeddings are above a cosine threshold
   (default `0.85`, ADR 0012). Read-only; no auto-merge. Skill-side
   pairs are flagged so the operator can route them through human
   review.
6. **LLM-judge scaffolding + audit log (ADR 0012 Layer 3, #66).**
   New `memstem hygiene dedup-judge` subcommand judges each candidate
   pair and writes one row per result to a `dedup_audit` table
   (schema v7) with `applied = 0`. Default judge is `NoOpJudge`
   (verdict `UNRELATED` for every pair) — opt into the real Ollama
   judge with `--enable-llm`. **No vault frontmatter is written from
   this PR.** A future resolution PR will read `applied = 0` rows and
   write `deprecated_by` / `valid_to` / `supersedes` / `links` for
   safe verdicts.

### Added — bounded preview mode for `dedup-candidates`

- **`memstem hygiene dedup-candidates --max-memories N`** caps the
  outer loop at the first N indexed memory ids (sorted by id), so
  the sweep finishes in O(M·N) instead of O(N²) and is bounded by a
  smoke-test timeout. The default behavior is unchanged: omitting
  the flag still runs a full scan. Same flag is plumbed through
  `dedup-judge` for consistency. The function-level
  `find_dedup_candidate_pairs(..., max_memories=...)` parameter is
  the supported way to call this from Python.
- **Why:** the function issues one `query_vec` per indexed memory,
  and `query_vec` is a vec0 k-NN MATCH that scans all of
  `memories_vec`. On a ~1k-memory vault that's ~30s; on Brad's
  production vault that exceeded a 45-second smoke timeout even with
  `--neighbors 1 --limit 1`. `--limit` only caps the *report*, not
  the work; the new flag caps the *work*.
- **Small efficiency win:** the per-memory metadata fetch is now
  one bulk query instead of N. Negligible on small vaults, a
  measurable win at production scale.
- 3 new tests in `TestMaxMemories` (cap bounds the outer loop,
  `max_memories=0` returns empty, `max_memories=None` matches the
  default full scan).

### Added — operational smoke test

- **`scripts/smoke_0_7_0.sh`** — read-only / dry-run production
  smoke test. Defaults: takes a `VAULT=/path/to/vault` env var,
  runs each new-in-0.7.0 sweep in its safest mode, never invokes
  the live Ollama judge, never applies importance bumps, and
  bounds `dedup-candidates` to a small `--max-memories` so it
  always returns inside the timeout. The only writes it can cause
  are the unavoidable `query_log` rows from a single search, which
  the hygiene config can disable per-vault if even that is too
  much. See `docs/operations.md` for the full procedure.

### Docs — `docs/operations.md`

- New "0.7.0 production smoke test" section with the six-step
  ladder (health, HTTP search, query_log / importance dry-run,
  distill, dedup-candidates bounded, dedup-judge warning), what
  each step asserts, and the explicit warning that
  `dedup-judge` writes to `dedup_audit` even with `NoOpJudge` —
  rows are always inserted; only the verdict differs.

### Added — LLM-judge scaffolding + audit log (ADR 0012 Layer 3)

- **New `memstem hygiene dedup-judge` subcommand** runs each candidate
  pair (from `dedup-candidates`) through a judge and writes an
  audit row to the new `dedup_audit` table (schema migration v7).
  **No vault mutations.** The future resolution PR will read
  `applied = 0` rows and apply safe verdicts to vault frontmatter
  (`deprecated_by` / `valid_to` / `supersedes` / `links`); until
  then this is purely an inventory + opinion step.
- **Default judge is `NoOpJudge`** — every pair gets verdict
  `UNRELATED` recorded with `judge = "noop"`. The operator opts
  into the real Ollama judge with `--enable-llm`. This means the
  default CLI run is safe in CI, on cron, and for users who don't
  want to spend LLM cycles.
- **`OllamaDedupJudge`** ships behind `--enable-llm`. It loads the
  prompt from `src/memstem/prompts/dedup_judge.txt` (ADR 0012's
  canonical text), calls `/api/generate` on the configured Ollama
  model, and parses strict-or-fenced JSON. Malformed responses
  fall back to `UNRELATED` with the raw text in the rationale —
  the audit log surfaces what went wrong; the sweep never crashes.
- **Tests use stub judges only.** `StubJudge` accepts canned
  verdicts; `OllamaDedupJudge` is exercised via a fake HTTP client
  passed to its constructor. **No real LLM is ever invoked from
  any test.**
- 28 new tests in `tests/test_hygiene_dedup_judge.py` covering the
  `Verdict` enum, `NoOpJudge`, `StubJudge`, `judge_pairs`
  orchestration (including the safe default), audit-log writes
  (one-row-per-result, multi-result batch, empty list, swallow on
  failure, `applied = 0` contract), `OllamaDedupJudge` with mocked
  HTTP for well-formed JSON / fenced JSON / garbage / empty / call
  errors / unknown verdict strings, the `_parse_response` helper
  with parametrized fixtures, the prompt-template-on-disk regression
  guard, and three CLI smoke tests including a "default does not
  mutate vault frontmatter" check.

### Added — near-duplicate candidate report (ADR 0012 Layer 2)

- **New `memstem hygiene dedup-candidates` subcommand** scans the
  vector index for memory pairs whose first chunk embeddings are
  cosine-similar above a threshold (default `0.85`, per ADR 0012)
  and prints them as an audit report. Read-only — does not delete,
  merge, mark, or write anything. The LLM-as-judge that turns
  candidates into definitive verdicts is Layer 3 / a future PR.
- **Pair canonicalization** so a→b and b→a collapse into one entry
  (`a_id < b_id` lexicographically); each pair appears exactly once
  with its true cosine computed from raw embeddings.
- **Skill safety flag** marks pairs where either side is a `skill`
  record so the operator can be extra-careful — ADR 0012 routes
  skill-vs-anything candidates through human review.
- 17 new tests in `tests/test_hygiene_dedup_candidates.py` covering
  empty state, near-identical vectors clustering, unrelated vectors
  filtered, threshold tightening/loosening, self-pair exclusion, the
  a/b canonicalization, the skill flag, sort-by-cosine-descending,
  the `--limit` truncation, the read-only contract, dataclass
  immutability, the documented default cosine threshold, and three
  CLI smoke tests.

### Added — distillation candidate report (ADR 0008 Tier 2, PR-D first slice)

- **New `memstem hygiene distill` subcommand** lists clusters of
  memories that *could* be summarized into a digest record. Read-only
  in this slice — no LLM calls, no vault mutation, no distillation
  records created. The LLM-driven distiller that consumes this
  report is a later PR behind an explicit config flag.
- **Two clustering strategies** ship in this slice:
  1. **Topic clusters** — memories sharing a tag of the form
     `topic:*` are grouped. `agent:*` tags are deliberately excluded
     (they'd produce one giant cluster per agent).
  2. **Daily-week clusters** — `type=daily` records from the same
     `agent:<x>` workspace within the same ISO calendar week.
- **Threshold:** `--min-cluster-size` (default 5, per ADR 0008 Tier 2).
- **Idempotent re-runs:** clusters whose every member is already
  linked from an existing `type=distillation` memory are filtered
  out so the report stays fresh.
- **`MemoryType.DISTILLATION` enum value** added so future PRs can
  validate distillation records without a schema change.
- 19 new tests in `tests/test_hygiene_distillation.py` covering empty
  vault, single-topic clustering above/below threshold, the no-cluster
  rule for `agent:*` tags, multi-topic vaults, daily-week clustering
  including the cross-week split case and the cross-agent split,
  the "already distilled" filter (full coverage skips, partial
  coverage keeps), the `skip_already_distilled=False` override, the
  read-only contract on the vault, candidate shape parity, ordering
  by size, and the CLI subcommand's banners and `--min-cluster-size`
  flag.

### Added — deterministic hygiene importance bumps (ADR 0008 Tier 1, PR-C)

- **New `memstem hygiene importance` subcommand** consumes the
  `query_log` written by PR-B and proposes conservative `importance`
  bumps for memories the user actually retrieved. Default is
  `--dry-run` (prints proposed changes; doesn't mutate); pass
  `--apply` to persist.
- **Per-row formula:** each `memstem_get` open contributes `0.05`,
  each search hit at rank `r` contributes `0.01 / r`, weighted at
  half for exposures older than 30 days. Per-record cap of `0.1`
  per sweep, final cap at `1.0`. Importance never decreases here —
  decay is a separate concern.
- **Skip rules:** records whose `valid_to` is in the past, whose
  `deprecated_by` is set, or that are already at `importance == 1.0`
  are not bumped. Phased-out content shouldn't earn weight.
- **Idempotence:** the cursor in the new `hygiene_state` table
  (schema migration v6) advances only on `--apply`, so dry-runs
  re-show the same proposals and reruns of `--apply` are no-ops
  until new log rows arrive.
- 21 new tests in `tests/test_hygiene_importance.py` covering the
  formula, the per-run and absolute caps, the skip rules, the
  unset-importance default, the recency penalty, the cursor
  advancement, the empty-plan-still-advances rule, and the CLI
  subcommand's dry-run vs apply behavior.

### Added — retrieval feedback logging (ADR 0008 Tier 1, PR-B)

- **Search now records per-hit exposure into a bounded `query_log`
  table** in `_meta/index.db` (schema migration v5). Each row carries
  `ts`, `kind` (`search` | `get`), `query`, `client` (`cli` | `mcp` |
  `http`), `memory_id`, `rank`, and `score`. The hygiene worker (next
  PR) reads this table to bump `importance` on memories the user
  actually retrieved.
- **Logging is opt-in per call site:** `Search.search(log_client=...)`
  enables it; the v0.6.x callers that don't pass `log_client` write
  nothing, so test fixtures and one-off internal calls are
  blast-radius-zero. The CLI / MCP / HTTP servers all opt in
  automatically when `hygiene.query_log_enabled = True` (default).
- **Logging never breaks search.** Every entry point is wrapped in
  `try/except` that downgrades errors to one warning and continues —
  a corrupt log table or schema-version drift will not silently mute
  `memstem_search`.
- **Boundedness:** the table caps at `hygiene.query_log_max_rows`
  (default 100k). When exceeded, the oldest rows are FIFO-pruned by
  `id` to ~90% of the cap, giving headroom for subsequent writes.
- 21 new tests in `tests/test_retrieval_log.py` covering the schema
  migration, per-hit row writes, get-row writes, FIFO pruning, the
  `max_rows=0` "never prune" sentinel, the swallow-all-failures
  contract, the score-after-importance contract, and the
  default-off behavior for un-instrumented callers.

### Added — importance-aware ranking (ADR 0008 Tier 1, PR-A)

- **Search now applies a small importance boost on top of RRF.** The
  formula is `final = rrf * (1 + alpha * importance)`, where `alpha`
  is the new `search.importance_weight` config knob (default `0.2`,
  per ADR 0008). Records without an explicit `importance` field are
  treated as a neutral `0.5` so un-annotated memories aren't penalized.
- **Why:** before this, every record competed on raw retrieval
  relevance only. A skill the user pinned at `importance=1.0` ranked
  the same as a one-off session note. With `alpha=0.2` the boost is a
  tiebreaker — close ranks can be flipped, but a strong relevance gap
  still wins. Tunable per-call (Search API), per-vault
  (`_meta/config.yaml`), or per-request (HTTP `POST /search`).
- **What it doesn't do:** it doesn't surface records that don't match
  the query at all (importance is a re-ranker, not a forcing
  function). It doesn't override the `valid_to` expiration filter
  (ADR 0011 PR-B). And `alpha=0.0` cleanly disables the feature,
  preserving the v0.1 RRF-only ordering.
- 11 new tests in `tests/test_search.py::TestImportanceRanking` and
  `TestSearchConfigImportance` cover the close-tie boost, the
  rank-gap dominance threshold, the unset-importance default, the
  `alpha=0` short-circuit, the multiplicative formula, and the
  config round-trip.

## [0.6.2] — 2026-04-28

### Fixed — installer now persists standard provider API env vars

- `scripts/install.sh` now falls back from `MEMSTEM_OPENAI_KEY`,
  `MEMSTEM_GEMINI_KEY`, and `MEMSTEM_VOYAGE_KEY` to the standard
  `OPENAI_API_KEY`, `GEMINI_API_KEY`, and `VOYAGE_API_KEY` names before
  calling `memstem auth set`. This prevents installs from working only
  in the original shell or MCP-specific environment while plain shell,
  cron, PM2, and systemd invocations report `embedder unavailable`.
- `memstem connect-clients` no longer warns about a missing shell env var
  when a provider key is already present in Memstem's persistent secret
  store.

## [0.6.1] — 2026-04-28

### Fixed — `memstem mcp` cold-start exceeded MCP client connection timeout

- **`memstem mcp` now resolves vault/index/embedder lazily**, on the first
  tool call instead of at server start. The MCP handshake (`initialize` +
  `tools/list`) used to wait for the SQLite + `sqlite-vec` index open,
  embedder initialisation, and vault scan before answering — for a vault
  with ~1k memories and a 250 MB+ index that can take ~32 s, just past
  OpenClaw's bundle-mcp `connectionTimeoutMs` default of 30 s. The
  symptom was `bundle-mcp: failed to start server "memstem" (memstem
  mcp): Error: MCP server connection timed out after 30000ms` repeating
  in the host gateway's logs and zero MemStem MCP tools available to
  the agent.
- **What changed:** `build_server()` accepts a new `resources=` kwarg
  pointing at a `_Resources` holder. The CLI's `mcp` command constructs
  a lazy holder (`_Resources.lazy(...)`) so the heavy work — index
  open, embedder bring-up, search composition — fires on the first
  `memstem_search` (or any tool) call. Subsequent calls reuse the
  cached resources. The eager `build_server(vault, index, embedder)`
  signature is unchanged; existing tests and the daemon's in-process
  embed continue to work without modification.
- **Trade-off:** the first tool call after spawning a fresh MCP
  subprocess pays the load cost (~30 s on Brad's vault), where it used
  to be paid up-front. After that, queries are fast and the
  subprocess stays warm until `cfg.mcp.idle_timeout_seconds` elapses.
  Net effect: the connection timeout class of failure goes away
  entirely, with no change to steady-state query latency.
- **Thread-safety:** `_Resources` uses double-checked locking so that
  two FastMCP worker threads racing on the very first tool call
  initialise each resource exactly once, not twice.

### Removed — Obsidian plugin scaffold

- **`clients/obsidian/`** (TypeScript scaffold) and **ADR 0010** are
  removed. The v0.6.0 scaffold only proved the integration loop
  end-to-end (status-bar daemon indicator); it didn't ship the
  promised search modal, sidebar pane, "New memory" command, or
  frontmatter scaffolding. Releasing it as-is would have been a brand
  promise the code didn't keep — the manifest claimed "Hybrid keyword +
  semantic search" but only delivered a connection-status pixel.
- **The HTTP API stays.** `GET /health`, `GET /version`, `POST /search`,
  `GET /memory/{id_or_path}` continue to be co-hosted in `memstem
  daemon`; they're useful infrastructure for any first-party client
  (CLI tools, future editor extensions). The `memstem-search` skill
  (PR #42) uses `POST /search` as one of its priority levels and
  continues to work.
- An Obsidian plugin will return as a dedicated future release with a
  feature set that matches the manifest copy.

### Added — embedder selection at install time

- **`memstem init` now accepts `--provider <name>`** to write a config
  pre-populated with sensible defaults for the chosen backend
  (model, dimensions, `api_key_env`). Known providers: `ollama`
  (default), `openai` (`text-embedding-3-large` @ 3072 dims),
  `gemini` (`gemini-embedding-2-preview` @ 768 dims), `voyage`
  (`voyage-3` @ 1024 dims).
- **`scripts/install.sh` now accepts `--embedder <name>` and
  `--openai-key` / `--gemini-key` / `--voyage-key`** so a single
  `curl … | bash` invocation can land MemStem with a cloud embedder
  configured and authenticated. Picking a non-Ollama embedder
  implies `--no-ollama` and `--no-model`. Keys are also pickable up
  from `MEMSTEM_OPENAI_KEY` / `MEMSTEM_GEMINI_KEY` /
  `MEMSTEM_VOYAGE_KEY` env vars (cleaner for unattended installs
  that want to keep keys off the command line). After install, the
  key is stored via `memstem auth set` so every subsequent `memstem`
  invocation on the box picks it up.
- 17 new tests — `tests/test_embeddings.py::TestForProviderFactory`
  (7 covering the factory), `tests/test_cli.py::TestInit` (5 covering
  the new `--provider` flag), `tests/test_install_sh.py::TestEmbedderValidation`
  (5 covering install.sh's embedder-name validation).

## [0.6.0] — 2026-04-28

Twelve PRs that together make the v0.x line ready for a public flip:
the **quality pipeline** (ADRs 0011 + 0012) — write-time noise filter,
exact-body dedup, TTL tagging, boot-echo hash filter — keeps the vault
from being polluted by the firehose of low-signal AI-session memories.
The **operational layer** — idle-timeout self-exit, Index locking with
WAL, OpenAI/Voyage batch chunking — kept the live daemon stable through
a Gemini → OpenAI embedder migration of ~1,085 records. The
**developer-facing layer** — first-party HTTP API for first-party
clients, `memstem-search` skill for Claude Code/OpenClaw, `memstem
auth` for persistent API keys, and a 15-second e2e smoke test —
removes the friction that was blocking external use.

### Added — `memstem auth` for persistent embedder API keys (#54, closes #41)

- **New command group `memstem auth set/show/remove`** persists API keys
  to `~/.config/memstem/secrets.yaml` (mode 0600, gitignore-irrelevant
  because it lives outside any vault). When the corresponding env var is
  not exported in the current shell, the embedder factory falls back to
  this file — so cron jobs, PM2 ecosystems, systemd units, and headless
  servers all work without each one needing its own export.
- **Resolution order:** the explicitly configured `embedding.api_key_env`
  wins; if unset, the provider's default (`OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `VOYAGE_API_KEY`); if still empty, the secrets file.
- **Why this matters:** previously, `memstem search` from a regular shell
  silently degraded to lexical-only when the env var was missing — same
  vault, same config, but worse results, with no obvious signal. The MCP
  server worked fine because Claude Code passed the key via its own env
  block. Issue #41 has the full reproduction; tactical CLI now lets users
  set the key once per machine instead of per-shell.
- **Test override:** `MEMSTEM_SECRETS_FILE` env var redirects the file
  path (used by the test suite for hermetic isolation; the global pytest
  fixture in `tests/conftest.py` points every test at a tmp path).
- 49 new tests — `tests/test_auth.py` (32 covering the module),
  `tests/test_cli.py::TestAuth` (12 covering the CLI), and
  `tests/test_embeddings.py::TestSecretsFileFallback` (5 covering the
  embedder fallback path).

### Added — end-to-end smoke test for the installed binary (#53)

- **`scripts/e2e-smoke.sh`** exercises the full happy path against a
  throwaway vault using the installed `memstem` binary: init, doctor,
  reindex, search, MCP stdio handshake (`initialize` + `tools/list` +
  `memstem_search` `tools/call`), and `connect-clients --dry-run`.
  Runs in ~15s, no network, no API keys.
- Pairs with `pytest` (component-level): this is the integration layer
  that catches binary-vs-source drift, schema regressions, and broken
  end-to-end wiring that unit tests miss.
- Designed to run before tagging a release, flipping the repo public,
  or accepting a meaningful PR. Suitable to wire into CI as a separate
  `e2e` job after `lint` / `test` pass.

### Added — idle-timeout self-exit for `memstem mcp` (#50, closes #40)

- **`memstem mcp` subprocesses now exit themselves after `idle_timeout`
  seconds with no tool calls** (default 1800 = 30 min, configurable via
  `mcp.idle_timeout_seconds` in `_meta/config.yaml`; `0` disables).
  Activity is tracked monotonically; a daemon thread polls every
  `timeout/10` seconds (clamped to 5–60s) and sends SIGTERM when idle
  exceeds the threshold. Claude Code transparently respawns on the
  next request — users never see the interruption.
- **Why it matters:** without this, every Claude Code session left an
  orphan `memstem mcp` behind; on a dev box that meant 5–13 stale
  processes by end of day, each holding its own SQLite connection.
  Lock contention between them was the primary trigger for the
  `database is locked` cascade fixed in #48 and #52.

### Added — boot-echo hash filter (#47)

- **A second-pass hash filter compares the SHA-256 of each session's
  first 1024 bytes against a curated set** of "I am Claude / I'm an AI
  assistant" boot-echo openings. Sessions whose head hashes into the
  set are dropped at the noise filter stage. The walker that builds
  the set lives in `core/extraction.py::build_boot_echo_hashes`, with
  skip-dirs and a max-depth cap (the speedup in #49 brought this from
  ~3.6s to ~20ms on the live vault).
- Implements ADR 0011 PR-C. With #44 (write-time noise filter) and
  #46 (TTL tagging), this completes the noise-filter trio.

### Added — TTL tagging for transient memory kinds (#46)

- **Memories whose `type` is in the transient set** (`session`, `daily`,
  `boot_echo`, …) now get a default `valid_to` set to
  `created + ttl_days` so the hygiene worker can age them out without
  needing a per-record decision. TTL values are configured per-kind in
  `hygiene.ttl_days` in `_meta/config.yaml`.
- Implements ADR 0011 PR-B. This unlocks the v0.2 hygiene worker
  (Phase 2) by giving it deterministic decay rules.

### Added — exact-body hash dedup, Layer 1 (#45)

- **The pipeline now suppresses any incoming record whose body has a
  hash collision** with an existing vault memory. Hashing is over a
  *normalized* body — whitespace runs collapsed, lowercased, stripped —
  so trivial formatting differences don't bypass the check. SHA-256
  hashes are stored in a `body_hash_index` table; an incoming write
  that hashes to an existing row increments a `seen_count` counter
  instead of creating a duplicate.
- Implements ADR 0012 Layer 1. ADR 0012's mem0 audit reference: one
  hallucinated fact re-entered the mem0 memory 808 times via the
  recall feedback loop. A single SHA-256 check collapses all 808 to
  one row with `seen_count = 808`. Layers 2 (embedding similarity) and
  3 (LLM-as-judge) ship in later PRs.

### Added — write-time noise filter (#44)

- **A heuristic noise filter runs in the pipeline before write-time**
  and drops session chunks whose content is below a length+entropy
  threshold or matches one of the boot-echo / system-message regex
  families. Keep / drop decisions are logged at INFO so a vault owner
  can audit what's being filtered.
- Implements ADR 0011 PR-A. Without this, the firehose of AI-session
  ingestion was producing several hundred low-signal memories per day,
  drowning the high-signal ones in search.

### Added — Claude Code / OpenClaw search skill

- **First-party `memstem-search` skill** under `clients/skills/memstem-search/`.
  A single `SKILL.md` with frontmatter compatible with both Claude Code's skill
  loader and OpenClaw's bundled-skill format (`metadata.clawdbot` and
  `metadata.openclaw` namespaces side by side). Installed by symlink or copy
  into the consumer's skill directory.
- **Why a skill in addition to the MCP:** Claude Code does not pre-load MCP
  tool schemas; they appear as deferred tools and must be loaded via
  `ToolSearch` before they can be called. Agents miss this step and skip
  MemStem even when configured. A skill is pre-listed in the session-start
  available-skills block, so the agent sees `memstem-search` immediately
  with no schema-loading dance.
- **Skill owns the full priority ladder.** The procedure tries
  MCP → HTTP `/search` (the daemon shipped above) → `memstem` CLI →
  grep, in order. Callers do not need to remember the order; invoking
  the skill is enough.
- Distribution to consumers stays manual in this PR (symlink/copy from
  `clients/skills/memstem-search/` into `~/.claude/skills/`,
  `<project>/.claude/skills/`, or `~/<openclaw-workspace>/skills/`).
  Automated install via `memstem connect-clients` lands in a follow-up.

### Added — local HTTP API for first-party clients

- **`memstem daemon` now co-hosts a local HTTP server** on
  `127.0.0.1:7821` (configurable via `http.port` in
  `_meta/config.yaml`). The server reuses the daemon's live `Vault`,
  `Index`, and `Embedder` instances — no per-query subprocess, no
  duplicate state. Endpoints mirror the MCP tool list one-to-one:
  `GET /health`, `GET /version`, `POST /search`, `GET /memory/{id_or_path}`.
  Loopback-only by design; v0.1 has no auth surface.
- **New deps:** `fastapi>=0.110.0`, `uvicorn>=0.30.0`. Imported lazily
  inside the daemon path so the CLI's other commands don't pay for
  them.
- 14 new tests cover the HTTP server (health/version/search/memory,
  type filtering, request-level RRF overrides, 404 handling).

### Fixed — `_backfill_embed_state` race on concurrent index opens

- **`Index._migrate()` no longer crashes with
  `IntegrityError: UNIQUE constraint failed: embed_state.memory_id`**
  when two connections (e.g. an MCP child and a CLI invocation) open
  the same vault simultaneously. Both SELECTs would return the same
  un-stamped rows; both would try to INSERT; the loser used to crash.
  Switched the helper's INSERT to `INSERT OR IGNORE` so the duplicate
  is silently skipped — the `NOT EXISTS` guard in the SELECT narrows
  the window but cannot close it.
- 2 new regression tests: a deterministic test that drives the
  helper's INSERT statement with a stale-view payload and verifies it
  doesn't raise, plus a source-level guard that asserts the SQL
  literally contains `INSERT OR IGNORE` so a future refactor cannot
  silently reintroduce the race.

### Fixed — serialize Index reads through lock + WAL/busy_timeout (#52)

- **`EmbedWorker._read_for_embed` now goes through `Index.get_path()`
  instead of `self.index.db.execute(...)`** — the unprotected direct
  call was racing the shared connection state under load and
  intermittently raising `sqlite3.InterfaceError: bad parameter or
  other API misuse`, which manifested as a "worker N crashed" cycle
  in the daemon.
- **The Index connection now opens with `journal_mode = WAL` and
  `busy_timeout = 5000`** so a CLI invocation (`memstem reindex`,
  `memstem embed`) opening its own connection no longer fails with
  `database is locked` while the daemon is writing.

### Fixed — chunk-batch OpenAI/Voyage requests + surface API errors (#51)

- **OpenAI `embed_batch` paginates inputs at `MAX_BATCH_SIZE = 100`**
  (Voyage at 128) so a single large record chunked at 2048 chars no
  longer hits the per-request token cap. Without this, the live
  Gemini → OpenAI migration hit `400 Bad Request` on the first
  ~1.5 MB record (≈ 750 chunks ≈ 380k tokens).
- **`httpx.HTTPStatusError` now surfaces the response body** for
  OpenAI/Voyage so oversized-batch and validation failures are
  diagnosable from the daemon log instead of just the bare status
  line.

### Fixed — speed up boot-echo walk with skip-dirs + max-depth pruning (#49)

- **The boot-echo discovery walk now skips `__pycache__`, `.git`,
  `node_modules`, `.venv`, etc., and prunes at `max_depth = 8`**.
  On a vault with the live Ari + Claude Code corpus the walk dropped
  from ~3.6s to ~20ms — about 180× faster — which kept the noise
  filter from being the long pole on daemon boot.

### Fixed — hold Index lock around pipeline-side write transactions (#48)

- **The `Pipeline.process()` write path now acquires the same
  `Index._lock` the Index methods use**, so a concurrent embed worker
  doesn't see a half-committed write or trip
  `database table is locked` mid-transaction. Surfaced together
  with #50 during the orphan-MCP cascade investigation.

### Docs — ADR 0011 + 0012, the quality pipeline (#43)

- **ADR 0011 — write-time noise filter and fact extraction.** Lays
  out the three-stage pipeline (heuristic filter, TTL tagging,
  boot-echo hash filter) implemented in #44, #46, and #47.
- **ADR 0012 — LLM-judge dedup, Layers 1–3.** Specifies exact-body
  hash dedup (Layer 1, #45), near-duplicate detection via embedding
  cosine (Layer 2, future), and LLM-as-judge for the remainder
  (Layer 3, future). Layer 1 ships now; 2 and 3 land in v0.7+.

## [0.5.0] — 2026-04-27

Four PRs shipped together that together close the loop on multi-agent
ingestion safety, OpenClaw transcript coverage, and configurable
ranking. The headline win: the daemon now ingests OpenClaw session
trajectories as full searchable transcripts (PR #36), so a search for
an exact phrase from yesterday's chat actually lands on the chat.
Combined with opt-in workspace discovery (PR #32), the per-workspace
layout schema (PR #33), and the search-config wiring fix (PR #35),
the v0.5.0 vault is a meaningfully better retrieval target than
v0.4.0 was — measured against a 12-query eval, top-5 went from 10/12
under the prior multi-agent install to 12/12 under the scoped + full-
transcript install.

### Changed — OpenClaw discovery is now opt-in

- **`memstem init` no longer auto-includes every OpenClaw workspace it
  finds.** On a multi-agent host (Ari + Blake + Charlie + …) the
  installer used to silently index all of them, mixing every agent's
  memory into one vault. The wizard now defaults each discovered agent
  to "no" — the user opts in explicitly, agent by agent. Shared files
  (`HARD-RULES.md`) follow the same opt-in model since they belong to a
  workspace.
- **`memstem init -y` (non-interactive) writes a Claude-Code-only config.**
  Previously `-y` meant "auto-include every discovered agent with
  content" — convenient but wrong on multi-agent installs. Now `-y`
  produces a conservative config; OpenClaw workspaces must be added by
  re-running `memstem init` interactively or by hand-editing
  `_meta/config.yaml`.
- **Existing installs are unaffected on disk** but should review their
  `agent_workspaces` list. To prune the index after removing entries
  from `config.yaml`, delete the corresponding directories under
  `<vault>/memories/openclaw/<tag>/` and `<vault>/daily/<tag>/` and
  re-run `memstem reindex`.

### Added — OpenClaw session trajectory ingestion

- **`*.trajectory.jsonl` files under a workspace's configured
  `session_dirs` are now ingested as `type:session` records.** Lets
  Memstem search the full transcript of every OpenClaw session, not
  just the distilled `[TECHNICAL]/[DECISION]/[RULE]` bullets that the
  upstream memory writer extracts. Search for an exact phrase from
  yesterday's chat now lands you on the exact session.
- New `OpenClawLayout.session_dirs` field — list of workspace-relative
  directories. Empty by default (opt-in). Set
  `["agents/main/sessions"]` for OpenClaw's standard layout.
- New `_parse_trajectory_file()` parses the OpenClaw event-log format,
  pulling `prompt.submitted.data.prompt` (user turns) and
  `model.completed.data.assistantTexts` (assistant turns) into a
  chronological transcript. Tool calls, context-compilation events,
  and trace artifacts are intentionally skipped — they're operational
  metadata that adds noise to a search index.
- Trajectory records carry `session_id`, `workspace_dir`, `agent_id`,
  `turn_count`, `created`, `updated` in metadata, with the agent tag
  applied by the workspace adapter (`agent:<tag>`).
- Watch loop also handles trajectory paths — incremental updates as
  the agent appends events get reflected in the index.
- 14 new tests covering parser correctness (turns, operational events,
  empty/malformed lines, metadata extraction), classification (in/out
  of session_dirs, suffix matching, default-empty), and end-to-end
  reconcile (default skip vs. opt-in include).

### Added — per-workspace layout overrides for OpenClaw

- **`OpenClawWorkspace` now accepts a `layout` field** specifying which
  paths inside the workspace get ingested. Lets toolkit users with
  non-canonical OpenClaw layouts (memories under `notes/` instead of
  `memory/`, skills disabled, custom `MEMORY.md` filename) point the
  adapter at their actual paths instead of forking the adapter or
  symlinking files.
- New `OpenClawLayout` model with five configurable fields, all with
  defaults that preserve current behavior:
  - `memory_md` — top-level core file (default `MEMORY.md`; `None` to skip).
  - `claude_md` — operational rules file (default `CLAUDE.md`; `None` to skip).
  - `memory_dirs` — list of directories whose `*.md` descendants are
    ingested (default `["memory"]`; empty list = no recursive ingestion).
  - `skills_dirs` — list of directories whose `**/SKILL.md` descendants
    are ingested (default `["skills"]`; empty list = no skills).
  - `session_dirs` — list of directories whose `*.trajectory.jsonl`
    descendants are ingested as session records (default `[]`; opt-in).
- Both reconcile (`_iter_workspace_files`) and watch
  (`_classify_workspace_path`) honor the layout, so live file events
  flow into the index using the same path conventions configured for
  reconcile.
- Existing configs are unchanged: omitting `layout` falls back to the
  canonical defaults via Pydantic's `default_factory`. 6 new tests cover
  the override paths (custom memory dir, skip MEMORY.md, skip skills,
  multiple memory dirs, default unchanged, watch classifier).

### Fixed — search config knobs are now actually read

- **`SearchConfig.rrf_k`, `bm25_weight`, and `vector_weight` were dead
  config.** The values lived in `_meta/config.yaml` but neither the CLI
  (`memstem search`) nor the MCP server read them — both call sites used
  the function defaults. So changing those values in config did nothing.
  Found while investigating ranking quality on a 12-query retrieval
  test: vault cleanup alone moved top-5 from 6/12 to 8/12, but tuning
  the (unused) weights showed no further improvement until the wiring
  was fixed.
- `Search.search()` now accepts `rrf_k`, `bm25_weight`, `vector_weight`
  parameters; the CLI threads `cfg.search.*` through, and the MCP
  server's `build_server()` accepts a `search_config: SearchConfig`
  kwarg that the daemon passes from the loaded config.
- `rrf_combine()` applies the weights as
  `score += weight / (k + rank)` per source. Default weights stay
  `1.0/1.0` so existing installs see no behavior change. Set
  `bm25_weight: 0` to make search vec-only, or vice versa. 5 new tests
  cover weight scaling, zero-weight short-circuit, and weighted
  overlap.

## [0.4.0] — 2026-04-26

Two related cutover fixes shipped together: the post-restart re-embed
storm (PR #30) and the MCP-spawned-child-has-no-API-key silent
BM25-only fallback (PR #31). Plus the `__init__.py` version string
catches up with `pyproject.toml` after drifting since 0.1.0. CI matrix
also updated to `actions/checkout@v6`, `actions/setup-python@v6`, and
`codecov/codecov-action@v6` via three dependabot bumps.

### Fixed — skip re-embed when content unchanged (PR #30, schema v3)

- **Pipeline no longer re-enqueues a record whose body and embedder
  signature haven't changed.** Earlier versions enqueued every record
  on every emit — so a `pm2 restart memstem` re-embedded all ~765
  records via the reconcile pass, even when no body had changed.
  Wasteful in time and (for API providers) in rate-limit quota.
  Schema v3 adds an `embed_state` table tracking the body hash +
  embedder signature each memory was last successfully embedded
  with; the pipeline checks this via the new `Index.needs_reembed`
  helper before enqueueing and skips when hash + signature both
  match. The worker writes a fresh `embed_state` row after every
  successful vector upsert. Net result: post-restart reconcile is a
  no-op for unchanged records.
- **Re-upserting a memory no longer cascade-deletes its child rows.**
  `Index.upsert` was using `INSERT OR REPLACE INTO memories`, which
  SQLite implements as DELETE-then-INSERT and so triggered
  `ON DELETE CASCADE` on `embed_state` and `embed_queue`. The
  practical effect was that the worker's hard-won "embedded" record
  evaporated on the next reconcile. Switched to `INSERT ... ON
  CONFLICT(id) DO UPDATE` so the row stays in place and child
  references survive.
- **Schema migration v3** is automatic on first connect; legacy
  databases get an `embed_state` row backfilled for every memory
  that already has vectors, with `embed_signature = NULL`. NULL is
  treated as "compatible with any signature" by `needs_reembed` so
  the upgrade doesn't trigger a global re-embed — the first time a
  body actually changes (or a user runs `memstem reindex`), the
  legacy NULL gets stamped with the real signature.
- 23 new tests covering the embed-state helpers, the pipeline skip
  path (unchanged body, changed body, signature change, no vectors
  yet), the worker's stamp-on-success behavior, and the v3 backfill
  (populates for vectorized memories, skips empty ones, doesn't
  clobber existing rows).

### Fixed — connect-clients propagates embedder API key into MCP env (PR #31)

- **The MCP entries written by `connect-clients` now include the
  embedder's API key.** Earlier versions wrote `"env": {}` for Claude
  Code and no `env` block at all for OpenClaw, so when those clients
  spawned `memstem mcp` as a subprocess, the child got no API key —
  the parent shell's env doesn't propagate to MCP children. The
  result was a silent fallback to BM25-only search: vectors were in
  the index, but every `memstem_search` result came back with
  `vec_rank: null` because `_maybe_embedder()` caught the
  `EmbeddingError` and built `Search(embedder=None)`.
- New `mcp_env_from_embedding(api_key_env)` helper in
  `integration.py` reads the configured `embedding.api_key_env` and
  resolves it against the install-time shell, returning a dict
  suitable for the MCP entry's `env` block. Empty for local
  providers (Ollama) and for missing/blank env vars.
- `register_mcp_server` and `register_openclaw_mcp_server` now accept
  an `env: dict[str, str] | None` kwarg that merges into the written
  entry's env block. Defensive-copy semantics — never mutates the
  module-level `DEFAULT_*_ENTRY` constants.
- `memstem connect-clients` resolves the API key once up front and
  threads it into both registration paths. Prints a one-line warning
  if the configured `api_key_env` is set in config but missing from
  the install shell, telling the user to export it and re-run.
- 13 new tests covering the helper (set/missing/blank/None/os.environ
  fallback), the Claude Code register path (env merges, default
  preserved when env=None, no mutation of constants, custom-entry
  + env compose), and the OpenClaw register path (env adds an
  otherwise-absent block, empty/None env preserves no-block default,
  no mutation).

### Fixed — version string mismatch

- `src/memstem/__init__.py` had been pinned to `__version__ = "0.1.0"`
  since the original 0.1.0 release in PR #22, while `pyproject.toml`
  was bumped to 0.2.0 (cdc4088) and 0.3.0 (000384b) without the
  matching `__init__.py` change. Now both files agree on `0.4.0`.
  Future release commits should bump both in the same diff.

### Changed — CI dependencies (dependabot PRs #1, #2, #3)

- `actions/setup-python` 5 → 6
- `codecov/codecov-action` 4 → 6
- `actions/checkout` 4 → 6

## [0.3.0] — 2026-04-26

### Added

- **`connect-clients` now registers Memstem MCP in each OpenClaw
  agent's `openclaw.json`.** Earlier versions only patched the
  agent's CLAUDE.md with the "use Memstem MCP first" directive — but
  if the agent's openclaw.json didn't have a `mcp.servers.memstem`
  entry, the directive was unhonorable: the agent looked for the
  MCP, didn't find it, and fell back to grep or CLI. Same shape of
  bug as the v0.2.0 Claude Code MCP-location fix, now closed for
  OpenClaw too.
- New `register_openclaw_mcp_server` helper in `integration.py` —
  reads/writes the agent's `mcp.servers.<name>` block while
  preserving every other key in the (large) `openclaw.json`. Same
  Change return type, same `.bak`, same dry-run semantics as
  `register_mcp_server`. Direct JSON edit (rather than shelling out
  to `openclaw mcp set`) keeps `integration.py` filesystem-only.
- New `openclaw_config_for_workspace` resolver — mirror of
  `claude_md_targets_for_openclaw` for the agent's OpenClaw config.
  Accepts a workspace dir, a CLAUDE.md path, or the openclaw.json
  itself.
- New `DEFAULT_OPENCLAW_MCP_SERVER_ENTRY` constant (`{command, args}`
  shape — OpenClaw's `mcp.servers` doesn't use Claude Code's `type`
  discriminator).
- 16 new tests covering the registration helper (entry shapes,
  preservation of other servers, idempotency, .bak, dry-run, missing
  files, malformed JSON, custom-entry override) and the workspace
  resolver (workspace dir / direct file / sibling lookup / missing
  cases).

## [0.2.0] — 2026-04-26

Cumulative release covering PRs #23–#29 plus the MCP location fix.
Shipped features: complete installer toolkit (`install.sh` +
`memstem doctor`), four pluggable embedder backends (Ollama / OpenAI /
Gemini / Voyage) with an always-on embed queue, Gemini default
`gemini-embedding-2-preview` with Matryoshka dimensions, thread-safe
SQLite Index, batch-size-aware Gemini calls, and the cutover
`connect-clients` registration moved to the location current Claude
Code releases actually read.

### Fixed — connect-clients MCP location

- **`connect-clients` was registering Memstem in a config file Claude
  Code no longer reads.** Earlier versions wrote the
  `mcpServers.memstem` entry to `~/.claude/settings.json`, but current
  Claude Code releases discover MCP servers from `~/.claude.json`
  (the file `claude mcp add` manages). The settings.json block was
  silently inert, so no Claude session — interactive or
  relay-spawned — actually picked up the Memstem MCP server. Sessions
  fell back to the `memstem` CLI via `Bash`, which works but skips
  the direct MCP path.
- `register_mcp_server` now writes the new entry shape (`type`,
  `command`, `args`, `env`) and `connect-clients` defaults to
  `~/.claude.json`. A new `remove_legacy_mcp_server` step cleans up
  the stale entry from `~/.claude/settings.json` automatically (with
  a `.bak`); `--legacy-settings PATH` overrides the location for
  testing.
- Six new tests covering the cleanup helper (file missing, entry
  absent, entry present alongside others, lone entry that empties
  the `mcpServers` key, dry-run, invalid JSON).

### Fixed (PR #29)

- **Gemini batch size limit.** `batchEmbedContents` caps requests at
  100 items per call; records with long bodies (~250KB daily logs)
  chunk into 100+ pieces and were hitting `400 Bad Request` on the
  live cutover. `GeminiEmbedder.embed_batch` now splits oversize
  inputs into sub-batches of `MAX_BATCH_SIZE` (=100) and
  concatenates results — same outward contract, multiple HTTP calls
  under the hood.
- **400 errors include the response body.** Gemini's error
  messages live in the JSON body and explain *why* (input too large,
  bad model, etc.). The bare HTTP status line was hiding them.
  `EmbeddingError` now surfaces the first 500 chars of the body.
- Two new tests covering the batch split (250 chunks → 3 calls of
  100/100/50) and the surfaced-error format.

### Fixed (PR #28)

- **Concurrent SQLite access from the embed worker.** `Index.connect()`
  opens with `check_same_thread=False` so the worker can run sync
  SQLite calls under `asyncio.to_thread`, but Python's `sqlite3`
  module isn't actually thread-safe on a single connection
  (concurrent commits race; the sqlite-vec extension keeps thread-
  local state). Added a `threading.RLock` around every Index read
  and write path. Workers can still run concurrently — the lock is
  cheap and only held during the SQLite call, not the embedder
  HTTP call.
- Two new pounding tests in `TestThreadSafety` confirm 16-way
  concurrent upserts + queue ops complete without `cannot commit -
  no transaction is active` or `bad parameter or other API misuse`
  errors. Without the lock, those errors hit within ~10 ops.

### Changed (PR #27)

- `GeminiEmbedder` default model is now `gemini-embedding-2-preview`
  (current best-quality Gemini embedding: ~20% recall improvement on
  heterogeneous corpora vs `gemini-embedding-001`, 8k context
  window, multimodal-capable). Google retired `text-embedding-004`
  (the previous default shipped in PR #26); the same API key works
  for the new model. Users who want maximum stability over absolute
  quality can pin `model: gemini-embedding-001` in
  `_meta/config.yaml` — that's the production-stable predecessor.
- `GeminiEmbedder` sends `outputDimensionality` for models that
  support Matryoshka representation (`gemini-embedding-001`,
  `gemini-embedding-2`, `gemini-embedding-2-preview`). This lets
  users keep an existing 768-dim Ollama schema and switch to Gemini
  without rebuilding the index — Gemini's native 3072d gets
  truncated server-side to whatever `dimensions` is configured.
- Gemini response width is validated against config; mismatches
  raise a clear `EmbeddingError` rather than silently corrupting
  the index.

### Added (PR #26)

- **Pluggable embedder backends** via a formal `Embedder` ABC and an
  `embed_for(EmbeddingConfig)` factory. Four implementations ship:
  `OllamaEmbedder` (default, local), `OpenAIEmbedder` (with
  `base_url` knob for OpenAI-compatible providers like Together,
  Mistral, Groq, vLLM, LM Studio), `GeminiEmbedder`
  (`text-embedding-004` — same 768d as Ollama, no reindex on switch),
  and `VoyageEmbedder` (Anthropic's recommended partner). API keys
  live in env vars named by `EmbeddingConfig.api_key_env`; nothing
  secret lands in the vault.
- **Always-on embed queue.** New `embed_queue` SQLite table (schema
  v2). The pipeline writes records synchronously and enqueues each
  one for embedding. `EmbedWorker` drains the queue with retry +
  backoff; failed records land in `failed=1` after `max_retries`
  (default 5) and surface in `memstem doctor`. The daemon runs the
  worker continuously alongside reconcile + watch; one-shot drains
  via `memstem embed`.
- **`memstem embed` CLI command** for manual queue drains.
  `--retry-failed` resets records that hit max retries.
- `EmbeddingConfig.workers` (default 2) and `batch_size` (default 8)
  tune queue throughput; CPU Ollama at 1, API providers at 4+.
- ADR 0009 documents the rationale and the architecture.

### Changed (PR #26)

- `pipeline.process` no longer embeds inline; ingest latency is now
  bounded by disk + SQLite, not by the embedder. The previous
  inline-embed path is gone.
- `memstem doctor` reports `Embed queue: N pending, M failed` so
  operators can see whether the queue is keeping up.
- `memstem doctor`'s embedder check now works for every provider
  (was Ollama-only).
- `memstem migrate --no-embed` and `install.sh --migrate-no-embed`
  are kept as no-op aliases for back-compat with PR #23/#24
  invocations — embedding is always deferred now.
- Schema migration tracker no longer accumulates extra rows on each
  migration; `schema_version` keeps exactly one row at the latest
  applied version.
- `Index.connect()` opens the SQLite connection with
  `check_same_thread=False` so the embed worker can run sync SQLite
  calls under `asyncio.to_thread`. Writes are still serialized by
  SQLite's single-writer lock.

### Fixed (PR #25)

- **Path collisions across agents.** Daily logs and skills with the
  same title/date from different agents collapsed into one record on
  disk. On Brad's box, 326 daily files reduced to 80, and ~130
  OpenClaw records were silently lost during the first cutover.
  Pipeline now extracts `agent:<tag>` from record tags and produces
  agent-scoped paths: `daily/<agent>/<date>.md`, `skills/<agent>/<slug>.md`,
  `memories/<source>/<agent>/<id>.md`. Records without an agent tag
  keep the legacy paths, so MCP-driven upserts and the FlipClaw
  migration's tag-less ingest path are unchanged.
- **Orphan rows in the index when paths rotate.** `Index.upsert` now
  detects when another row already holds the target `path` under a
  different id and cleans up that row's tags/links/FTS/vec entries
  before inserting the new one. Previously these orphans accumulated
  in the FTS5 table and surfaced as "hit X missing from memories
  table" warnings during search.

### Added

- `memstem migrate` is now a top-level CLI command (was previously
  only reachable via `scripts/migrate-from-flipclaw.py`). Same flags:
  `--apply`, `--days`, `--vault`, `--openclaw`, `--claude-root`,
  plus new `--no-embed` and `--progress-every`. The script wrapper
  still works unchanged.
- `memstem migrate --no-embed` skips vector embedding during the bulk
  import. Records still land in vault + FTS5; run `memstem reindex`
  later to backfill vectors. This is the practical answer for
  CPU-only Ollama where bulk embedding queues up tens of seconds
  per chunk and saturates the runner.
- `memstem migrate --progress-every N` prints a heartbeat every N
  records during `--apply` (default 25, 0 to silence).
- `install.sh --migrate` runs `memstem migrate --apply` after init so
  a fresh box ends up with history imported.
- `install.sh --migrate-days N` overrides the Claude Code session
  lookback window (default 30). Smaller values cut the embed load on
  fresh installs — older sessions can land via the daemon's watch
  loop over time.
- `install.sh --migrate-no-embed` passes `--no-embed` through to
  `memstem migrate`. The recommended pattern for a fresh install on
  CPU-only Ollama: `--migrate --migrate-no-embed --start-daemon`,
  then run `memstem reindex` overnight to backfill vectors.
- `install.sh --start-daemon` starts `memstem daemon` under PM2 (no-op
  with a warning if PM2 isn't installed). Combined with
  `--connect-clients`, the installer is a single-shot cutover.
- Ollama service health check in `install.sh`: after install, polls
  `http://localhost:11434/api/tags` until the daemon responds (up to
  30s). On macOS, attempts `brew services start ollama` first.
- `install.sh --connect-clients` now prints a dry-run diff before
  applying, so the operator sees what's about to change.
- Smoke tests for `install.sh` (`tests/test_install_sh.py`): `bash -n`
  syntax check, `--help` flag-coverage check, unknown-flag rejection.

### Changed

- Default `OllamaEmbedder` timeout bumped from 30s → 120s. The 30s
  default was too tight under bulk-ingest load: a fresh `migrate
  --apply` queues many large chunks against a CPU-only runner, and
  individual embed calls were timing out before they ever reached
  the head of the queue. 120s is generous in steady state and
  recoverable in bulk.

### Fixed

- `install.sh --yes` now propagates `-y` to `memstem init`, so an
  unattended install no longer hangs at the setup wizard's per-agent
  prompts.

## [0.1.0] - 2026-04-XX

First tagged release. Phase 1 v0.1 — running on the live EC2 box,
ingesting from Claude Code + multi-agent OpenClaw, exposing MCP
search, with FlipClaw retired. Tag date is filled in at release
time after Brad validates the cutover.

### Added

- Initial repo scaffold (README, ARCHITECTURE, ROADMAP)
- Architecture Decision Records (ADRs 0001-0008)
- Source skeleton for `memstem` package (core, adapters, hygiene, servers)
- Frontmatter specification and MCP API specification
- CI workflow, issue/PR templates, contributing guide
- MIT license, security policy
- `memstem.core.frontmatter`: typed `Frontmatter` model, `parse`, `serialize`,
  and `validate` helpers conforming to `docs/frontmatter-spec.md`
- `memstem.core.storage`: `Vault` class with `read`, `write`, `walk`, `delete`;
  typed `Memory` model wrapping frontmatter + body + vault-relative path
- `memstem.core.embeddings`: `OllamaEmbedder` HTTP client (uses `/api/embed`)
  with single + batch methods, paragraph-aware `chunk_text` helper, and a
  `requires_ollama` pytest marker registered for integration tests
- `memstem.core.index`: SQLite + FTS5 + sqlite-vec hybrid index with
  versioned migrations, `upsert` / `upsert_vectors` / `delete`, and
  `query_fts` / `query_vec` returning typed `FtsHit` / `VecHit` records;
  cascading deletes for tags/links/vectors and a wikilink extractor
- `memstem.core.search`: `Search` orchestrator for hybrid retrieval —
  Reciprocal Rank Fusion over BM25 + vector hits, materializing typed
  `Result` records (memory + score + per-source ranks) from the vault.
  Sanitizes FTS5-special characters from natural-language queries; falls
  back to BM25-only if the embedder errors so the daemon never goes mute
- `memstem.adapters.openclaw`: `OpenClawAdapter` reads Ari/OpenClaw
  markdown files (memory, daily logs, skills) into normalized
  `MemoryRecord` objects. Reconcile walks paths once; watch streams
  records via `watchdog` inotify. Classifies files by name (`SKILL.md`,
  `YYYY-MM-DD.md`, else memory) and falls back to filename/H1 for titles
  when frontmatter is absent
- `memstem.adapters.claude_code`: `ClaudeCodeAdapter` reads Claude Code
  session JSONL files into one `MemoryRecord` per session (type=session).
  Body is the concatenated user/assistant transcript with tool blocks
  summarized (`[tool_use: Bash]`, `[tool_result]`) so it stays readable.
  Title falls back from `ai-title` → first user prompt → session UUID.
  Re-emits the full session on file change; pipeline upserts by `ref`
- `memstem.servers.mcp_server`: `build_server(vault, index, embedder=None)`
  factory returning a `FastMCP` instance with five tools matching the
  spec in `docs/mcp-api.md`: `memstem_search`, `memstem_get`,
  `memstem_list_skills`, `memstem_get_skill`, `memstem_upsert`. Auto-
  generates vault paths on upsert when none is supplied (memories /
  skills / sessions / daily layouts)
- `memstem.core.pipeline`: `Pipeline` converts adapter-emitted
  `MemoryRecord` objects into canonical `Memory` writes — stable id per
  `(source, ref)`, vault write, index upsert, embed-and-store chunks
- CLI commands (`memstem init|daemon|search|reindex|mcp`) wired up via
  Typer. `init` scaffolds a vault and `_meta/config.yaml`; `daemon`
  runs OpenClaw + Claude Code adapters into the pipeline (reconcile +
  watch); `search` and `reindex` operate on the local vault; `mcp`
  serves the FastMCP tools on stdio for Claude Code et al.
- `memstem.migrate` + `scripts/migrate-from-flipclaw.py`: one-shot
  migration that walks `~/ari/memory/`, `~/ari/skills/`, and recent
  Claude Code sessions, tags every record with `flipclaw-migration`,
  and runs them through the standard pipeline. Default is dry-run
  (counts + sample preview); `--apply` writes
- Multi-agent OpenClaw support: `OpenClawWorkspace(path, tag)`,
  `OpenClawAdapterConfig(agent_workspaces, shared_files)`,
  `ClaudeCodeAdapterConfig(project_roots, extra_files)`, all wired
  through `Config.adapters`. The adapter walks per-agent
  `MEMORY.md` / `CLAUDE.md` / `memory/*.md` / `skills/*/SKILL.md`,
  tagging records with `agent:<tag>` (plus `core` for MEMORY.md and
  `instructions` for CLAUDE.md). Shared files (e.g. HARD-RULES.md)
  emit with a `shared` tag instead. Legacy paths-only mode preserved
  for back-compat
- `scripts/install.sh`: one-line installer for an unattended install
  (`curl ... | bash -s -- --yes`). Verifies Python 3.11+, installs
  pipx and memstem, optionally installs Ollama and pulls
  `nomic-embed-text`, scaffolds the vault, runs `memstem doctor` to
  confirm. `--no-ollama`, `--no-model`, `--vault`, `--from-git`,
  `--connect-clients`, `--remove-flipclaw` knobs
- `memstem doctor`: CLI command that verifies Python version, vault +
  config existence, index health, embedder reachability, and every
  configured adapter target (OpenClaw workspaces / shared files,
  Claude Code roots / extras). Exits non-zero if any check fails
- `memstem.discovery`: auto-discovery helpers for OpenClaw agent
  workspaces (`~/*/openclaw.json`), shared rules files (`HARD-RULES.md`),
  Claude Code session roots (`~/.claude/projects`), and per-user
  Claude Code instructions (`~/.claude/CLAUDE.md`). Each candidate
  carries a content count so the installer can highlight non-empty
  agents
- `memstem init` setup wizard: defaults to interactive per-candidate
  prompts; `-y` / `--non-interactive` auto-includes every candidate
  with content. `--home <path>` lets tests and headless installs scope
  the discovery to a sandbox
- `ClaudeCodeAdapter` accepts `extra_files`. Each is read as a
  markdown instructions file and emitted as a record with the
  `instructions` tag (type=memory). Reconcile yields them alongside
  session JSONLs; watch picks up changes via the parent dir
- `memstem.integration`: idempotent wiring of Memstem into client
  config. `register_mcp_server` adds a `mcpServers.memstem` block to a
  Claude Code `settings.json` (preserving other servers).
  `apply_directive` inserts or updates a versioned
  `<!-- memstem:directive v1 -->` block in a CLAUDE.md, leaving
  surrounding content untouched. `remove_flipclaw_hook` strips the
  legacy `claude-code-bridge.py` SessionEnd hook. Each edit writes a
  `.bak` and supports `dry_run` to preview a unified diff
- `memstem connect-clients` CLI command wraps the above. Defaults patch
  `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, and the CLAUDE.md
  in every workspace from the vault config. `--openclaw <path>` is
  repeatable; `--remove-flipclaw` disables the legacy bridge;
  `--dry-run` previews; `--settings` and `--claude-md` override paths
  for tests and non-default installs
- ADR 0007: remote-machine ingestion is out of scope until Phase 3+;
  documented sync-and-watch as the recommended workaround
- ADR 0008: tiered-memory design (importance scoring, distillations,
  hygiene worker) for v0.2. Status proposed; no code lands until Brad
  reviews

### Changed

- `Adapter.watch` and `Adapter.reconcile` are declared without `async`
  in the ABC so subclass async generators type-check cleanly
- CI test matrix runs Linux at full strictness; macOS and Windows are
  marked experimental for visibility-only
