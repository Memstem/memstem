#!/usr/bin/env python3
"""Read-only source audit; pipeline/search verification in a throwaway vault.

Never writes the OpenClaw databases, bridge, installed skills, or live vault.
Reports contain counts and session IDs, not conversation text. An optional JSON
report can be retained privately. No daemon or embedding service is required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from memstem.adapters.openclaw import OpenClawAdapter, _trajectory_to_record
from memstem.adapters.openclaw_sqlite import discover_databases, read_database
from memstem.adapters.trajectory import merge_transcripts
from memstem.config import OpenClawLayout, OpenClawWorkspace
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Vault


async def verify(args: argparse.Namespace) -> dict[str, Any]:
    ws = OpenClawWorkspace(
        path=args.state_root.resolve(),
        tag=args.tag,
        layout=OpenClawLayout(
            memory_md=None,
            claude_md=None,
            memory_dirs=[],
            trajectory_sqlite_roots=[Path(".")],
            plugin_skill_roots=args.plugin_root,
            plugin_skill_allowed_roots=args.allow_plugin_root,
        ),
    )
    comparisons = []
    identities = 0
    with tempfile.TemporaryDirectory(prefix="memstem-openclaw-verify-") as directory:
        vault = Vault(Path(directory))
        index = Index(vault.root / "_meta/index.db", dimensions=4)
        index.connect()
        pipeline = Pipeline(vault, index)
        try:
            databases = discover_databases(ws)
            for db in databases:
                for native in read_database(db, ws):
                    sid = native.metadata["session_id"]
                    bridge_path = (
                        args.bridge_root
                        / "agents"
                        / db.parent.parent.name
                        / "sessions"
                        / f"{sid}.trajectory.jsonl"
                    )
                    bridge = _trajectory_to_record(bridge_path)
                    if bridge is None:
                        comparisons.append({"session": sid, "bridge": False})
                        continue
                    before = pipeline.process(bridge)
                    pipeline.process(native)
                    pipeline.process(bridge)  # simulate bridge lag after native
                    if before is not None:
                        after = vault.read(f"sessions/{sid}.md")
                        assert after.id == before.id
                        assert merge_transcripts(after.body, before.body) == after.body
                        assert merge_transcripts(after.body, native.body) == after.body
                        identities += 1
                    comparisons.append(
                        {
                            "session": sid,
                            "bridge": True,
                            "equal": bridge.body == native.body,
                            "bridge_preserved": merge_transcripts(native.body, bridge.body)
                            == native.body,
                            "native_chars": len(native.body),
                            "bridge_chars": len(bridge.body),
                        }
                    )
            # Same real adapter as production, with native disabled for this skill pass.
            ws.layout.trajectory_sqlite_roots = []
            adapter = OpenClawAdapter([ws])
            skills = [r async for r in adapter.reconcile([]) if r.metadata["type"] == "skill"]
            for record in skills:
                pipeline.process(record)
            for record in skills:
                pipeline.process(record)
            search = Search(vault, index)
            for record in skills:
                results = search.search(
                    record.title or Path(record.ref).parent.name, types=["skill"], limit=1000
                )
                assert any(r.memory.frontmatter.title == record.title for r in results), record.ref
            report = {
                "databases": len(databases),
                "sessions_compared": len(comparisons),
                "canonical_identities_verified": identities,
                "skills_discovered": len(skills),
                "skills_indexed": index.db.execute(
                    "SELECT count(*) FROM memories WHERE type='skill'"
                ).fetchone()[0],
                "skills_all_searchable": True,
                "comparisons": comparisons,
            }
            assert all(c.get("bridge_preserved", True) for c in comparisons)
            assert report["skills_indexed"] == len(skills), (
                "skill identity collision or unexpected duplicate"
            )
            return report
        finally:
            index.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--tag", default="openclaw")
    parser.add_argument("--bridge-root", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, action="append", default=[])
    parser.add_argument("--allow-plugin-root", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(verify(args))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "comparisons"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
