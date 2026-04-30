"""Recall-quality eval harness — loaders, scoring, reporting.

A query set is a YAML file shaped like::

    queries:
      - id: factual_ari_port
        class: factual          # one of: factual conceptual procedural historical
        query: what gateway port does Ari run on
        expect:
          body_contains: ["18789"]
          path_contains: ["CLAUDE.md"]
        top_k: 10               # optional; defaults to DEFAULT_TOP_K

Each ``expect`` is a logical-OR matcher across the result's title,
body, and path (case-insensitive substring). A query "found" the answer
when at least one of the top-k results matches its expect block.

Metrics:

- **MRR** — mean reciprocal rank of the first matching result, averaged
  across queries; 0.0 when not found in top-k.
- **Recall@K** — fraction of queries with at least one matching result
  in the top-K (K=3 and K=10 reported by default).
- **Per-class breakdown** — same metrics scoped to each of the four
  query classes the recall plan targets.

The harness never logs to ``query_log`` (uses ``log_client=None``) so
running the eval doesn't pollute live retrieval signals.

Note on FTS5 token coverage: BM25-only setups (e.g. when no embedder
is configured, or in unit tests) require every query token to appear
somewhere in a candidate document because FTS5's MATCH operator
defaults to AND. Production runs against a vault with an embedder
relax this — vec retrieval matches semantically related documents
even when wording differs. Eval queries should still be written to
exercise both signals; the per-class breakdown surfaces gaps in
either pipeline.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from memstem.core.search import Result, Search

VALID_CLASSES: tuple[str, ...] = ("factual", "conceptual", "procedural", "historical")
DEFAULT_TOP_K = 10


@dataclass(frozen=True, slots=True)
class ExpectMatcher:
    """Logical-OR substring matcher across a result's title, body, and path.

    A result matches if ANY configured substring (case-insensitive) is
    found in ANY configured field. All-empty matchers are rejected by
    :func:`load_queries` rather than silently scoring zero.
    """

    title_contains: tuple[str, ...] = ()
    body_contains: tuple[str, ...] = ()
    path_contains: tuple[str, ...] = ()

    def matches(self, result: Result) -> bool:
        title = (result.memory.frontmatter.title or "").lower()
        body = result.memory.body.lower()
        path = str(result.memory.path).lower()
        return (
            any(sub.lower() in title for sub in self.title_contains)
            or any(sub.lower() in body for sub in self.body_contains)
            or any(sub.lower() in path for sub in self.path_contains)
        )


@dataclass(frozen=True, slots=True)
class EvalQuery:
    """One eval query with its expected-answer matcher."""

    id: str
    class_: str
    query: str
    expect: ExpectMatcher
    top_k: int = DEFAULT_TOP_K


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Outcome of running one query through :class:`Search`."""

    query: EvalQuery
    rank: int | None
    """1-based rank of the first matching result; ``None`` if no match in top_k."""
    top_k: int
    elapsed_ms: float

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.rank if self.rank else 0.0

    @property
    def found(self) -> bool:
        return self.rank is not None


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregated metrics for one eval run."""

    total: int
    found: int
    mrr: float
    recall_at_3: float
    recall_at_10: float
    elapsed_ms: float
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    per_query: list[QueryResult] = field(default_factory=list)


def load_queries(path: Path) -> list[EvalQuery]:
    """Parse a YAML query set into typed :class:`EvalQuery` records.

    Validation rules:

    - Top-level must contain a ``queries`` list.
    - Each query must have a ``query`` string and a ``class`` from
      :data:`VALID_CLASSES`.
    - ``expect`` must specify at least one of ``title_contains`` /
      ``body_contains`` / ``path_contains`` (a query that matches
      nothing is a misconfiguration, not a "0 score" we silently swallow).
    - ``top_k`` defaults to :data:`DEFAULT_TOP_K`.

    Raises :exc:`ValueError` with a descriptive message on any violation
    so a malformed query set fails fast rather than producing meaningless
    metrics.
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_queries = data.get("queries", [])
    if not isinstance(raw_queries, list):
        raise ValueError(f"queries: expected list, got {type(raw_queries).__name__}")

    out: list[EvalQuery] = []
    for i, raw in enumerate(raw_queries):
        if not isinstance(raw, dict):
            raise ValueError(f"queries[{i}]: expected mapping, got {type(raw).__name__}")
        qid = str(raw.get("id") or f"q{i}")
        cls = str(raw.get("class") or "")
        if cls not in VALID_CLASSES:
            raise ValueError(
                f"queries[{qid}]: class must be one of {list(VALID_CLASSES)}, got {cls!r}"
            )
        query = str(raw.get("query") or "")
        if not query:
            raise ValueError(f"queries[{qid}]: query string is required")
        expect_raw = raw.get("expect") or {}
        if not isinstance(expect_raw, dict):
            raise ValueError(
                f"queries[{qid}]: expect must be a mapping, got {type(expect_raw).__name__}"
            )
        title_contains = tuple(str(s) for s in (expect_raw.get("title_contains") or ()))
        body_contains = tuple(str(s) for s in (expect_raw.get("body_contains") or ()))
        path_contains = tuple(str(s) for s in (expect_raw.get("path_contains") or ()))
        if not (title_contains or body_contains or path_contains):
            raise ValueError(
                f"queries[{qid}]: expect must specify at least one of "
                "title_contains / body_contains / path_contains"
            )
        matcher = ExpectMatcher(
            title_contains=title_contains,
            body_contains=body_contains,
            path_contains=path_contains,
        )
        top_k_raw = raw.get("top_k")
        top_k = int(top_k_raw) if top_k_raw is not None else DEFAULT_TOP_K
        if top_k < 1:
            raise ValueError(f"queries[{qid}]: top_k must be >= 1, got {top_k}")
        out.append(EvalQuery(id=qid, class_=cls, query=query, expect=matcher, top_k=top_k))
    return out


def run_query(search: Search, query: EvalQuery) -> QueryResult:
    """Execute one query against :class:`Search` and find the first matching rank.

    Passes ``log_client=None`` so running the eval doesn't bump
    importance on whatever happens to surface — the eval reads live
    state, it shouldn't change it.
    """
    start = time.perf_counter()
    results = search.search(query.query, limit=query.top_k, log_client=None)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    rank: int | None = None
    for i, result in enumerate(results, start=1):
        if query.expect.matches(result):
            rank = i
            break
    return QueryResult(query=query, rank=rank, top_k=query.top_k, elapsed_ms=elapsed_ms)


def run_eval(search: Search, queries: Sequence[EvalQuery]) -> EvalReport:
    """Run every query and aggregate per-class + overall metrics."""
    start = time.perf_counter()
    per_query = [run_query(search, q) for q in queries]
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    total = len(per_query)
    found = sum(1 for r in per_query if r.found)
    mrr = sum(r.reciprocal_rank for r in per_query) / total if total else 0.0
    recall_at_3 = (
        sum(1 for r in per_query if r.rank is not None and r.rank <= 3) / total if total else 0.0
    )
    recall_at_10 = (
        sum(1 for r in per_query if r.rank is not None and r.rank <= 10) / total if total else 0.0
    )

    by_class: dict[str, list[QueryResult]] = defaultdict(list)
    for r in per_query:
        by_class[r.query.class_].append(r)
    per_class: dict[str, dict[str, float]] = {}
    for cls, results in by_class.items():
        n = len(results)
        if n == 0:
            continue
        per_class[cls] = {
            "count": float(n),
            "found": float(sum(1 for r in results if r.found)),
            "mrr": sum(r.reciprocal_rank for r in results) / n,
            "recall_at_3": sum(1 for r in results if r.rank is not None and r.rank <= 3) / n,
            "recall_at_10": sum(1 for r in results if r.rank is not None and r.rank <= 10) / n,
        }

    return EvalReport(
        total=total,
        found=found,
        mrr=mrr,
        recall_at_3=recall_at_3,
        recall_at_10=recall_at_10,
        elapsed_ms=elapsed_ms,
        per_class=per_class,
        per_query=per_query,
    )


def report_to_json(report: EvalReport) -> dict[str, Any]:
    """Convert an :class:`EvalReport` to a JSON-serializable dict.

    Useful for diffing across runs (``run_eval --json-out``) and for
    feeding CI gates that compare a PR's metrics against the base
    branch.
    """
    return {
        "total": report.total,
        "found": report.found,
        "mrr": report.mrr,
        "recall_at_3": report.recall_at_3,
        "recall_at_10": report.recall_at_10,
        "elapsed_ms": report.elapsed_ms,
        "per_class": report.per_class,
        "per_query": [
            {
                "id": r.query.id,
                "class": r.query.class_,
                "query": r.query.query,
                "rank": r.rank,
                "top_k": r.top_k,
                "elapsed_ms": r.elapsed_ms,
                "found": r.found,
            }
            for r in report.per_query
        ],
    }


def format_report(report: EvalReport) -> str:
    """Human-readable summary of an eval run.

    Includes the headline aggregates, a per-class table, and a list of
    failed queries (so the operator can see which queries didn't find an
    answer in top-K).
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("MEMSTEM EVAL HARNESS — RECALL-QUALITY METRICS")
    lines.append("=" * 60)
    lines.append(f"Total queries:    {report.total}")
    lines.append(f"Found:            {report.found}/{report.total}")
    lines.append(f"MRR:              {report.mrr:.3f}")
    lines.append(f"Recall@3:         {report.recall_at_3:.3f}")
    lines.append(f"Recall@10:        {report.recall_at_10:.3f}")
    lines.append(f"Elapsed:          {report.elapsed_ms:.0f}ms")
    lines.append("")
    lines.append("Per-class breakdown:")
    lines.append(
        f"  {'class':12s} {'n':>3s}  {'found':>5s}  {'mrr':>5s}  {'r@3':>5s}  {'r@10':>5s}"
    )
    lines.append(f"  {'-' * 50}")
    for cls in sorted(report.per_class):
        m = report.per_class[cls]
        lines.append(
            f"  {cls:12s} {int(m['count']):>3d}  {int(m['found']):>5d}  "
            f"{m['mrr']:>5.3f}  {m['recall_at_3']:>5.3f}  {m['recall_at_10']:>5.3f}"
        )
    lines.append("")
    failed = [r for r in report.per_query if not r.found]
    if failed:
        lines.append(f"Failed queries ({len(failed)}):")
        for r in failed:
            lines.append(f"  [{r.query.class_:12s}] {r.query.id}: {r.query.query!r}")
    return "\n".join(lines)
