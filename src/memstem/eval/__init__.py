"""Recall-quality evaluation harness.

Loads a YAML query set, runs each query through :class:`Search`, and
reports MRR + Recall@K (per-query, per-class, aggregate). Used to gate
recall-quality PRs (see ``RECALL-PLAN.md``) — a PR that regresses MRR
by more than 3% relative is blocked from merging without human override.
"""

from memstem.eval.harness import (
    DEFAULT_TOP_K,
    VALID_CLASSES,
    EvalQuery,
    EvalReport,
    ExpectMatcher,
    QueryResult,
    format_report,
    load_queries,
    report_to_json,
    run_eval,
    run_query,
)

__all__ = [
    "DEFAULT_TOP_K",
    "VALID_CLASSES",
    "EvalQuery",
    "EvalReport",
    "ExpectMatcher",
    "QueryResult",
    "format_report",
    "load_queries",
    "report_to_json",
    "run_eval",
    "run_query",
]
