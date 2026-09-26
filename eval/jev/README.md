# Jev reranking trial

This experiment answers: **does adding Jev after MemStem retrieval improve the
evidence returned enough to justify its added latency, cost and failure modes?**
It does not turn on the production reranker. Session-summary verification is a
separate experiment. The runnable implementation is
`src/memstem/eval/jev_trial.py`; the operator CLI is `scripts/jev_trial.py`.

## What is ready

- Runnable preparation, capture, blind-review, locking, API evaluation and report commands.
- Twenty fictional examples covering ten failure categories, including late-session
  evidence and instructions embedded in retrieved text. They check the apparatus,
  not whether Jev improves the real vault.
- Fault-injection and metric correctness tests. Synthetic data and development
  results can never yield a positive rollout verdict.
- Live-memory labels must be assembled and independently reviewed before a real
  verdict. Do not substitute keyword matching or Jev's own grades for this work.

## Pre-registered experiment

Start with 320 distinct real query texts drawn uniformly from the retained search
log. Remove monitoring probes, evaluation-generated queries, secrets and malformed
requests **before any model results are visible**. Keep an exclusion log with a
reason for every removal. Replace excluded examples by continuing the same seeded
sample; never choose replacements because a system answered them poorly.

The query log has one row per result, not one per search. The sampler deduplicates
query text and does not mistake row count for search frequency. This is an
evaluation of distinct questions; it is not a traffic-weighted monthly forecast.

Target **80 development queries and at least 200 held-out queries**, with remaining
queries reserved for replacements before locking. Assign related paraphrases,
the same incident/decision, and copies of the same underlying source to a single
`group`. Split by group, not by individual wording. At least 100 independent groups
must appear in the test set. Development is where prompts, excerpts, confidence
thresholds and policies may be adjusted. Then lock them and run the holdout once.
Repeating calls for latency or order sensitivity is predeclared and is not a new
independent question. Changes after looking at test results require a new holdout.

Have at least ten real held-out cases in each category:

| Category | Example distinction to test |
| --- | --- |
| Exact fact | Correct port or identifier versus a nearby but different setting |
| Procedure | Approved operational steps versus a proposal or obsolete commands |
| Decision reason | Recorded rationale versus speculation about why something happened |
| Historical | What was true at a specified earlier time |
| Current state | Latest confirmed decision, including reversals and undeployed plans |
| Long session | Useful evidence well beyond the first 4,000 characters |
| Similar projects | Correct client, host or product rather than a similarly named one |
| Paraphrase | Natural wording and dictated terminology rather than source keywords |
| Multiple sources | Several pieces of evidence needed to answer one question |
| No answer | Supplied candidate pool does not establish the answer |

The sample is a practical starting point, **not a guarantee of statistical power**.
The grouped confidence intervals may still say the result is uncertain. Do not keep
adding cases and checking until a desired result appears; pre-register a second
fixed-size experiment if the first is inconclusive. Rare safety failures need more
data than a few hundred examples to establish low absolute risk.

## Freeze the evidence, then label it

For each question, record the query, requested/as-of time, normal top-ten ranking,
and a wider pool (default twenty). The pool is the wider result list plus any normal
top-ten results it missed. Fetch each full canonical body and hash it. This union
prevents a pool-size change from silently removing a baseline answer.

Capture requires an already-disabled reranker in the selected vault configuration
and verifies the daemon's vault identity. It uses normal loopback HTTP searches;
it does not send `rerank_top_n: 0`, because that request parameter is clamped to
one by the current server. Confirm the daemon loaded the recorded configuration.
The daemon can append its ordinary retrieval telemetry. It does not alter
configuration, enable a model, delete memories, or write evaluation scores back
into the vault. Two searches are made per question; pool-size-dependent MMR and
retrieval can differ, which is why `pool_order` is a separate control. Captures
are frozen per query, not an atomic snapshot of the entire evolving vault.

Before accepting a capture, record the daemon version, relevant non-secret search
settings and embedding model, verify the selected vault, and check that the source
bodies did not change across the two calls. Set `capture_verified` only after this
review. Degraded retrieval is retained and reported; it cannot pass the rollout
gate. Obtain a new capture in a separate experiment if the baseline was degraded.
Use an isolated snapshot for long campaigns where corpus churn is material.

Reviewers see a shuffled document packet, with no baseline positions, model scores
or arm names. Grade **every candidate**, including obvious negatives:

- **0:** unrelated, wrong entity/time, or contradicted for this question.
- **1:** related background but no answer.
- **2:** evidence answering part of the question.
- **3:** explicit, direct answer for the requested entity and time.

Every positive grade needs an exact quotation from the frozen body. All grades need
a rationale. Mark `harmful` separately when returning the item as the primary answer
would mislead: superseded settings, proposed work described as done, wrong client,
or injected evaluator instructions. A document can be useful historical evidence
for one question and harmful current guidance for another.

Search authoritative sources independently of the candidate pool to find missed
answers. Add known relevant records outside the pool to the label map with their
source quotation and rationale. They contribute to the retrieval ceiling and true
recall denominator even though reranking cannot invent/retrieve them. “No answer”
means no supported answer in the judged evidence scope, not proof that the whole
vault contains no answer. Record the searched scope in the rationale.

Use a reviewer other than Jev. Independently double-review at least 20% of real
test questions, and all current-state, harmful, disputed and no-answer labels.
Resolve disagreements against dated primary records **before** running the test.
Save both original judgments and the adjudication log. `second_reviewer` is an
attestation of actual independent review, not a field to fill with another name.
The automatic gate checks its coverage; it cannot establish reviewer honesty.

## Five arms, one primary comparison

| Arm | Purpose |
| --- | --- |
| `baseline` | Actual normal top ten captured with production search settings |
| `pool_order` | Wider pool's existing order, without Jev; isolates pool-size effects |
| `lexical` | Cheap deterministic word-overlap reordering; sanity control |
| `jev_head` | Jev sees a bounded beginning of each document |
| `jev_passages` | Jev sees query-selected passages under the same input budget |

**Primary:** `jev_passages` versus `baseline`. The other arms are diagnostics, not
additional opportunities to choose a statistically favorable winner on the holdout.
Compare `jev_passages` to `jev_head` to identify passage-selection benefits; compare
to `pool_order` and `lexical` to determine whether model inference adds anything.

This is explicitly **post-retrieval reranking**, after MemStem's existing MMR.
It does not claim to reproduce the old pre-MMR hook. A production integration must
match the tested order or be evaluated separately. Jev cannot repair missing
candidates; score ceiling failures as retrieval failures rather than model failures.

Jev receives opaque document IDs in shuffled order. Gold labels, baseline scores,
baseline ranks, reviewers and expected answers are never sent. The same source
IDs, dates, text budget and rubric are used across the two Jev arms. The payload
is bounded to 30,000 serialized bytes as a conservative context/cost guard; the
excerpt budget shrinks uniformly if needed. Long-session retrieval still needs
evaluation: the passage selector uses lexical overlap and cannot guarantee that
every relevant semantic passage will be selected.

Run each Jev arm three times per question with different deterministic input
permutations, without local result caching, interleaving arms and questions with
a seeded shuffle. Measure the real wall-clock API time. Keep every timeout,
rate-limit, malformed response, partial response and cost. Use one pinned model
identifier and record the returned resolved version; mixed versions invalidate
a positive verdict. No automatic API retries disguise first-attempt failures.

On any scoring failure, preserve the exact captured normal ranking. Never convert
a failed candidate score into zero and silently bury the candidate.

## Measurements and decision rules

Report real and fictional results separately. Average repetitions within each
query; bootstrap paired differences by source/topic group (3,000 draws, fixed seed,
95% percentile intervals). Report per-category means and the worst regressions.

- **nDCG@10:** primary graded relevance metric.
- **MRR@10:** how high the first substantive answer appears.
- **Hit@3 / Hit@10:** fraction of answerable questions with an answer in that range.
- **Recall@10:** fraction of all judged substantive evidence records retrieved.
  This is deliberately different from hit rate. Labels outside the pool matter.
- **Harmful top-one results:** whether misleading evidence became the lead result.
- **No-answer false acceptance / answerable false abstention:** evaluate the score
  threshold separately. The experiment records an abstention signal; it does not
  suppress production answers.
- **Order stability:** fraction of questions with identical top three across
  the three permutations; not an independent accuracy sample.
- **Probability calibration:** Brier score for the model's probability of a
  substantive answer versus independently judged relevance. A low score is better.
- **Added latency:** p50 and p95 for Jev including failures. This is incremental
  model time, not measured full live-search latency.
- **Billed cost:** cost per 1,000 reranked searches, separately from capture/embed
  costs. Missing API cost and errors use a conservative reservation and prevent a
  positive verdict. Total experiment spend includes both Jev arms and repeats.

Default gates are fixed in `PROTOCOL` before test access:

1. At least **+0.05 absolute nDCG@10**, with the paired 95% interval above zero.
2. MRR@10 and Hit@10 lower confidence bounds no worse than **−0.02**.
3. **No new harmful top-one regression** in a held-out question.
4. Added reranking **p95 ≤1 second**.
5. Mean model cost **≤$1 per 1,000 reranked searches**.
6. Fallback rate **≤1%**.
7. No category's mean nDCG@10 drops by more than **0.05 absolute**. Treat small
   category samples as diagnostic and inspect individual regressions too.
8. Minimum real sample/group/category counts, independent review coverage, verified
   non-degraded capture, three repeats, known costs and a stable model version.

The default 30,000-byte reservation at $0.042/M input tokens is $0.00126 per
attempt; actual typical cost should be much lower. For 280 real questions × two
arms × three repeats the conservative model reservation is **$2.1168**. A $3 run
budget accommodates that reservation. This excludes embedding calls during capture
and any reviewer model costs. Verify pricing before execution; the reservation is
a client stop condition, not a provider-side billing cap.

Output verdicts:

- **GO_TO_SHADOW:** offline criteria met; run a separate observation-only live
  check before requesting production approval.
- **NO_GO:** complete evidence failed one or more predeclared criteria. Inspect
  confidence intervals to distinguish clear degradation from an unproven gain.
- **INSUFFICIENT_EVIDENCE:** missing/too-small/unreviewed/degraded evidence, development
  only, fictional only, unstable model, or unknown billed cost. Do not call this success.

Before any production recommendation, inspect every harmful and substantial
per-category regression. Then observe at least 500 real searches over multiple
time windows, covering normal ingest activity and provider slowness. Measure actual
end-to-end p50/p95, failure recovery, result utility and query mix. Do not add
separate model latencies to historical p95 values and call that measured p95.
This live shadow stage is a later operator action, not implemented as an automatic
daemon or a scheduled job by this evaluation kit.

## Commands

Run from the repository with its development environment. All private data stays
under ignored `eval/jev/private/`; never put query bodies or labels in a public PR.
Output files are owner-only and commands refuse to overwrite completed evidence.

```bash
# No network: sample query text from the retained log through SQLite mode=ro.
.venv/bin/python scripts/jev_trial.py sample \
  --index /path/to/vault/_meta/index.db --count 320 \
  --output eval/jev/private/sample.json

# Review/exclude/group the sample and set split + category before this command.
.venv/bin/python scripts/jev_trial.py capture \
  --manifest eval/jev/private/reviewed-queries.json --vault /path/to/vault \
  --output eval/jev/private/captured.json
.venv/bin/python scripts/jev_trial.py blind \
  --dataset eval/jev/private/captured.json --output eval/jev/private/ratings.json

# Fill the ratings packet, independently review, adjudicate, then import it.
.venv/bin/python scripts/jev_trial.py label \
  --dataset eval/jev/private/captured.json --ratings eval/jev/private/ratings.json \
  --output eval/jev/private/labeled.json
.venv/bin/python scripts/jev_trial.py lock \
  --dataset eval/jev/private/labeled.json --output eval/jev/private/lock.json

# OPENROUTER_API_KEY must already be in the environment; never echo it.
.venv/bin/python scripts/jev_trial.py run \
  --dataset eval/jev/private/labeled.json --lock eval/jev/private/lock.json \
  --split dev --repeats 3 --budget-usd 3 --output eval/jev/private/dev-v1

# If development changes anything, finalize a fresh lock before accessing test.
.venv/bin/python scripts/jev_trial.py run \
  --dataset eval/jev/private/labeled.json --lock eval/jev/private/lock.json \
  --split test --repeats 3 --budget-usd 3 --output eval/jev/private/test-v1
.venv/bin/python scripts/jev_trial.py report \
  --dataset eval/jev/private/labeled.json --run eval/jev/private/test-v1 \
  --output eval/jev/private/test-v1/report.json

# Fictional connectivity check, explicitly incapable of proving real accuracy.
.venv/bin/python eval/jev/make_fixtures.py
.venv/bin/python scripts/jev_trial.py lock \
  --dataset eval/jev/fixtures.json --output eval/jev/private/smoke-lock.json
.venv/bin/python scripts/jev_trial.py run \
  --dataset eval/jev/fixtures.json --lock eval/jev/private/smoke-lock.json \
  --split dev --repeats 3 --budget-usd .20 --output eval/jev/private/smoke
```

For external gold records not returned in the candidate pool, append their source
ID and reviewed label to the labeled dataset before locking. Preserve full evidence
and source references privately. The blind packet covers the pooled candidates;
independent answer discovery is a separate review step.

Interrupted runs keep an append-only `attempts.jsonl`. They cannot generate a valid
complete report and are not silently resumed. Retain that evidence, investigate,
and create a separately named run; include aborted-run costs in the final accounting.

## Sources verified September 26, 2026

- [OpenRouter model and rate card](https://openrouter.ai/typesafe/jev-1.13)
- [Decisions API schema](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)
- [TypeSafe scoring rubric](https://docs.typesafe.ai/primitives/score)
- [Confidence interpretation](https://docs.typesafe.ai/confidence)

Confidence describes the model's reported distribution; it is not proof of
correctness. A fast, cheap run on fictional examples is API validation, not a
MemStem quality benchmark.
