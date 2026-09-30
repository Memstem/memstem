#!/usr/bin/env python3
"""Summarize the Jev shadow-reranking ledger (ADR 0044).

Operational view only: volume, failures, latency, cost and how often Jev
would have changed what was served. Whether its changes are *better* needs
relevance labels on a sample (see ADR 0044, "Evaluation").

    python scripts/jev_shadow_report.py [--vault ~/memstem-vault] [--since 2026-09-26]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def fmt_ms(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"p50 {pct(values, 0.5):.0f} ms, p95 {pct(values, 0.95):.0f} ms (n={len(values)})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path.home() / "memstem-vault")
    parser.add_argument("--since", default="0000-00-00", help="UTC date, inclusive")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    path = args.vault / "_meta" / "jev-shadow.db"
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM shadow_runs WHERE day >= ?", (args.since,))]
    ok = [r for r in rows if r["status"] == "ok"]

    top1_same = top1_in_served = 0
    overlap3: list[float] = []
    for r in ok:
        served = json.loads(r["served_ids"])
        jev = json.loads(r["jev_order"])
        top1_same += bool(served) and jev[0] == served[0]
        top1_in_served += jev[0] in served
        k = min(3, len(served))
        overlap3.append(len(set(jev[:k]) & set(served[:k])) / k if k else 0.0)

    served_rows = [r for r in rows if r.get("mode") == "served"]
    summary = {
        "ledger": str(path),
        "since": args.since,
        "runs": len(rows),
        "days": sorted({r["day"] for r in rows}),
        "mode": dict(Counter(r.get("mode") or "shadow" for r in rows)),
        "served_mode": {
            # ADR 0047: rows where Jev's order actually reached the caller.
            "runs": len(served_rows),
            "status": dict(Counter(r["status"] for r in served_rows)),
            "fallback_pct": (
                round(100 * sum(r["status"] != "ok" for r in served_rows) / len(served_rows), 2)
                if served_rows
                else None
            ),
            "added_latency_prep_plus_api": fmt_ms(
                [(r["prep_ms"] or 0) + (r["api_ms"] or 0) for r in served_rows if r["prep_ms"]]
            ),
        },
        "status": dict(Counter(r["status"] for r in rows)),
        "clients": dict(Counter(r["client"] for r in rows)),
        "errors": dict(Counter((r["error"] or "").split(":")[0] for r in rows if r["error"])),
        "degraded_runs": sum(r["degraded"] for r in rows),
        "latency": {
            "idle_wait": fmt_ms([r["wait_ms"] for r in rows if r["wait_ms"] is not None]),
            "prep": fmt_ms([r["prep_ms"] for r in rows if r["prep_ms"]]),
            "jev_api_ok": fmt_ms([r["api_ms"] for r in ok if r["api_ms"]]),
            "prep_plus_api_ok": fmt_ms([r["prep_ms"] + r["api_ms"] for r in ok if r["api_ms"]]),
        },
        "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 4),
        "cost_per_1000_ok": (
            round(1000 * statistics.mean(r["cost_usd"] for r in ok), 3) if ok else None
        ),
        "unknown_cost_runs": sum(1 for r in rows if not r["cost_known"]),
        "would_change": {
            "top1_differs_pct": round(100 * (1 - top1_same / len(ok)), 1) if ok else None,
            "jev_top1_outside_served_pct": (
                round(100 * (1 - top1_in_served / len(ok)), 1) if ok else None
            ),
            "mean_top3_overlap_pct": round(100 * statistics.mean(overlap3), 1) if ok else None,
        },
        "candidates_per_run": (
            round(statistics.mean(r["n_candidates"] for r in ok), 1) if ok else None
        ),
        "excerpt_budgets": dict(Counter(r["excerpt_budget"] for r in ok)),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    for key, value in summary.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
