#!/usr/bin/env python3
"""Prepare, freeze and evaluate a Jev reranking experiment. See eval/jev/README.md."""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from memstem.core.storage import Vault
from memstem.eval.jev_trial import (
    CATEGORIES,
    PROTOCOL,
    digest,
    lock_dataset,
    read_json,
    run_trial,
    summarize,
    validate,
    write_json,
)


def sample_queries(database: Path, count: int, seed: int) -> dict[str, Any]:
    """Read-only sample of distinct user query text; source/topic grouping is manual."""
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT query, MAX(ts), COUNT(*) FROM query_log "
            "WHERE kind='search' AND query IS NOT NULL AND query != '' GROUP BY query"
        ).fetchall()
    # Count(*) counts result rows, NOT independent searches. Never frequency weight by it.
    queries: dict[str, tuple[str, str]] = {}
    for query, timestamp, _ in rows:
        key = " ".join(query.casefold().split())
        queries.setdefault(key, (query, timestamp))
    selected = list(queries.values())
    random.Random(seed).shuffle(selected)
    return {
        "sampling": "Uniform distinct logged query texts; not traffic-frequency weighted. Review before capture.",
        "seed": seed,
        "queries": [
            {
                "id": f"real-{i:04}",
                "query": query,
                "last_seen": timestamp,
                "group": "TODO-source-topic-family",
                "split": "TODO-dev-or-test",
                "category": "TODO",
                "origin": "real",
            }
            for i, (query, timestamp) in enumerate(selected[:count])
        ],
    }


def capture(manifest: dict[str, Any], vault: Path, url: str, pool: int) -> dict[str, Any]:
    """Normal HTTP reads may append ordinary retrieval telemetry; never change settings."""
    if not 10 <= pool <= 30:
        raise ValueError("Pool must be 10..30")
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        raise ValueError("Capture endpoint must be loopback")
    for q in manifest["queries"]:
        if (
            q["split"] not in {"dev", "test"}
            or q["category"] not in CATEGORIES
            or q["group"].startswith("TODO")
        ):
            raise ValueError("Complete topic grouping, split and categories before capture")
    store = Vault(vault)
    config = yaml.safe_load((vault / "_meta/config.yaml").read_text())
    search_config = config.get("search", {})
    if search_config.get("reranker", {}).get("enabled", False):
        raise ValueError("Capture requires an already-disabled production reranker")
    cases = []
    with httpx.Client(timeout=60) as client:
        health = client.get(url + "/health")
        health.raise_for_status()
        health_data = health.json()
        # Server versions expose either vault_path or vault. Verify identity;
        # do not capture a different tenant just because it answered loopback.
        reported_vault = health_data.get("vault_path", health_data.get("vault"))
        if not isinstance(reported_vault, str) or Path(reported_vault).resolve() != vault.resolve():
            raise ValueError("Health endpoint does not verify the selected vault")
        for query in manifest["queries"]:
            snapshots = []
            for limit in (10, pool):
                started = time.perf_counter()
                response = client.post(
                    url + "/search",
                    json={"query": query["query"], "limit": limit},
                )
                response.raise_for_status()
                snapshots.append((response.json(), (time.perf_counter() - started) * 1000))
            baseline, baseline_ms = snapshots[0]
            wider, wider_ms = snapshots[1]
            candidates = []
            seen = set()
            for hit in [*wider, *baseline]:
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                # Read canonical content locally rather than issuing logged get calls.
                path = Path(hit["path"])
                resolved = (vault / path).resolve()
                if not resolved.is_relative_to(vault.resolve()):
                    raise ValueError("Candidate escaped selected vault")
                memory = store.read(path)
                if str(memory.id) != hit["id"]:
                    raise ValueError("Memory changed during capture")
                candidates.append(
                    {
                        "id": hit["id"],
                        "title": hit["title"] or "",
                        "body": memory.body,
                        "body_sha256": digest(memory.body),
                        "path": hit["path"],
                        "updated": hit["frontmatter"].get("updated"),
                    }
                )
            cases.append(
                {
                    **query,
                    "as_of": datetime.now(UTC).isoformat(),
                    "candidates": candidates,
                    "baseline": [h["id"] for h in baseline],
                    "baseline_ms": baseline_ms,
                    "pool_retrieval_ms": wider_ms,
                    "degraded": any(h.get("embedder_degraded") for h in [*baseline, *wider]),
                    "capture_verified": False,
                    "labels": {},
                    "reviewed_blind": False,
                }
            )
    dataset = {
        "schema": 1,
        "capture": {
            "health": health_data,
            "search_settings": {k: v for k, v in search_config.items() if k != "reranker"},
            "reranker_enabled": False,
            "pool": pool,
            "notes": "Two baseline/pool retrievals per query. Verify config/version and body stability before marking capture_verified.",
        },
        "cases": cases,
    }
    validate(dataset, labeled=False)
    return dataset


def blind_packet(dataset: dict[str, Any]) -> dict[str, Any]:
    validate(dataset, labeled=False)
    packet: dict[str, Any] = {
        "dataset_sha256": digest(dataset),
        "instructions": "Grade independently of any model output. No baseline ranks are shown. Add evidence and rationale. Consult authoritative sources for time-sensitive answers.",
        "cases": [],
    }
    for case in dataset["cases"]:
        docs = list(case["candidates"])
        random.Random("blind:" + case["id"]).shuffle(docs)
        packet["cases"].append(
            {
                "id": case["id"],
                "query": case["query"],
                "as_of": case.get("as_of"),
                "reviewer": "",
                "second_reviewer": "",
                "capture_verified": False,
                "documents": [
                    {
                        "id": d["id"],
                        "title": d["title"],
                        "body": d["body"],
                        "updated": d.get("updated"),
                        "grade": None,
                        "harmful": None,
                        "evidence": "",
                        "rationale": "",
                    }
                    for d in docs
                ],
            }
        )
    return packet


def apply_labels(dataset: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    if packet["dataset_sha256"] != digest(dataset):
        raise ValueError("Labels were written against a different frozen dataset")
    reviewed = {c["id"]: c for c in packet["cases"]}
    if set(reviewed) != {c["id"] for c in dataset["cases"]} or len(reviewed) != len(
        packet["cases"]
    ):
        raise ValueError("Missing/duplicate query labels")
    for c in dataset["cases"]:
        r = reviewed[c["id"]]
        labels = {
            d["id"]: {k: d[k] for k in ("grade", "harmful", "evidence", "rationale")}
            for d in r["documents"]
        }
        if len(labels) != len(r["documents"]):
            raise ValueError("Duplicate document labels")
        c.update(
            labels=labels,
            reviewer=r["reviewer"],
            second_reviewer=r.get("second_reviewer", ""),
            capture_verified=r["capture_verified"],
            reviewed_blind=True,
            answerable=any(d["grade"] >= 2 for d in labels.values()),
        )
    validate(dataset)
    return dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="Read-only query-log sampling; no network")
    sample.add_argument("--index", type=Path, required=True)
    sample.add_argument("--count", type=int, default=320)
    sample.add_argument("--output", type=Path, required=True)
    capture_parser = sub.add_parser("capture", help="Capture candidates from loopback search")
    capture_parser.add_argument("--manifest", type=Path, required=True)
    capture_parser.add_argument("--vault", type=Path, required=True)
    capture_parser.add_argument("--url", default="http://127.0.0.1:7821")
    capture_parser.add_argument("--pool", type=int, default=20)
    capture_parser.add_argument("--output", type=Path, required=True)
    for name in ("blind", "label", "lock"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--dataset", type=Path, required=True)
        cmd.add_argument("--output", type=Path, required=True)
        if name == "label":
            cmd.add_argument("--ratings", type=Path, required=True)
        if name == "lock":
            cmd.add_argument("--protocol", type=Path)
    run = sub.add_parser("run", help="Paid Jev evaluation; never changes production search")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--split", choices=["dev", "test"], required=True)
    run.add_argument("--budget-usd", type=float, required=True)
    run.add_argument("--repeats", type=int, default=3)
    report = sub.add_parser("report")
    report.add_argument("--dataset", type=Path, required=True)
    report.add_argument("--run", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command != "run" and args.output.exists():
        parser.error("Output already exists; refusing to overwrite experiment evidence")
    if args.command == "sample":
        value = sample_queries(args.index, args.count, PROTOCOL["seed"])
    elif args.command == "capture":
        value = capture(read_json(args.manifest), args.vault, args.url, args.pool)
    elif args.command == "blind":
        value = blind_packet(read_json(args.dataset))
    elif args.command == "label":
        value = apply_labels(read_json(args.dataset), read_json(args.ratings))
    elif args.command == "lock":
        value = lock_dataset(
            read_json(args.dataset), read_json(args.protocol) if args.protocol else PROTOCOL
        )
    elif args.command == "run":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            parser.error("Set OPENROUTER_API_KEY; keys are never written to trial output")
        result = run_trial(
            read_json(args.dataset),
            read_json(args.lock),
            args.output,
            api_key=key,
            split=args.split,
            budget_usd=args.budget_usd,
            repeats=args.repeats,
        )
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
        return
    else:
        value = summarize(
            read_json(args.dataset),
            read_json(args.run / "lock.json"),
            read_json(args.run / "results.json"),
        )
    write_json(args.output, value)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
