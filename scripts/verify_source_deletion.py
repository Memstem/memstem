#!/usr/bin/env python3
"""End-to-end verification for ADR 0026 (source-deletion tombstone).

Exercises the REAL OpenClaw adapter (not a test fake) against a throwaway
workspace, driving the full pipeline → sweep → search path the daemon uses:

  1. seed a workspace (memory, daily, skill, session) and reconcile it in
  2. confirm search finds an authored memory
  3. delete the memory's source file, run the sweep (as reconcile does)
  4. confirm search no longer returns it, it carries `deleted_at`, and
     `include_deleted=True` still surfaces it (recoverable)
  5. delete a daily log → tombstoned; delete a session `.jsonl` → NOT tombstoned
  6. restore the memory file, reconcile + sweep, confirm it returns to search

Runs BM25-only (no embedder needed). Prints PASS/FAIL per check and exits
non-zero on any failure, so it can gate a merge.

    python scripts/verify_source_deletion.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from memstem.adapters.base import Adapter
from memstem.adapters.openclaw import OpenClawAdapter
from memstem.cli import _sweep_deleted_sources
from memstem.config import OpenClawLayout, OpenClawWorkspace
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Vault

_FAILED = 0


def check(label: str, ok: bool) -> None:
    global _FAILED
    mark = "PASS" if ok else "FAIL"
    if not ok:
        _FAILED += 1
    print(f"  [{mark}] {label}")


async def reconcile(adapter: Adapter, pipe: Pipeline) -> None:
    async for record in adapter.reconcile([]):
        pipe.process(record)


def search_ids(search: Search, query: str, *, include_deleted: bool = False) -> set[str]:
    results = search.search(query, limit=10, include_deleted=include_deleted)
    return {str(r.memory.frontmatter.id) for r in results}


def ids_by_basename(index: Index) -> dict[str, tuple[str, str | None]]:
    """basename(ref) -> (memory_id, type), captured from record_map.

    Captured BEFORE a sweep, because the sweep deletes the dead ref row when
    it tombstones — so the memory can only be re-located afterward by its id.
    """
    out: dict[str, tuple[str, str | None]] = {}
    for _source, ref, memory_id, mtype in index.all_source_mappings():
        out[Path(ref).name] = (memory_id, mtype)
    return out


def deleted_at_of(vault: Vault, index: Index, memory_id: str) -> object:
    path = index.get_path(memory_id)
    if path is None:
        return "NO_INDEX_ROW"
    return vault.read(path).frontmatter.deleted_at


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ms-verify-"))
    vault_root = tmp / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (vault_root / sub).mkdir(parents=True, exist_ok=True)
    vault = Vault(vault_root)
    index = Index(tmp / "index.db", dimensions=8)
    index.connect()
    pipe = Pipeline(vault, index)
    search = Search(vault=vault, index=index, embedder=None)

    # --- seed a realistic OpenClaw workspace ---------------------------------
    ws_root = tmp / "ari"
    mem_file = ws_root / "memory" / "deploy-notes.md"
    daily_file = ws_root / "memory" / "2026-04-25.md"
    skill_file = ws_root / "skills" / "deploy" / "SKILL.md"
    session_file = ws_root / "sessions" / "s1.trajectory.jsonl"
    for f, body in [
        (mem_file, "# Deploy notes\n\nThe quokkafrazzle deploy uses rsync over ssh."),
        (daily_file, "# Daily 2026-04-25\n\nzlorptastic standup log entry."),
        (skill_file, "# Deploy skill\n\nscope: deploying\nverification: it runs"),
    ]:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    # Real OpenClaw trajectory event format (one JSON event per line).
    import json

    session_file.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    "type": "session.started",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "sessionId": "s1",
                    "workspaceDir": str(ws_root),
                    "data": {"agentId": "main"},
                },
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"prompt": "flibberwidget session question one"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:02.000Z",
                    "data": {"assistantTexts": ["flibberwidget session answer two"]},
                },
                {"type": "session.ended", "ts": "2026-04-26T23:00:07.000Z", "data": {}},
            ]
        )
        + "\n"
    )

    adapter = OpenClawAdapter(
        workspaces=[
            OpenClawWorkspace(
                path=ws_root,
                tag="ari",
                # session_dirs is opt-in (empty by default); enable it so the
                # session log is really ingested and the type-guard is exercised.
                layout=OpenClawLayout(session_dirs=["sessions"]),
            )
        ]
    )
    adapters: dict[str, Adapter] = {adapter.name: adapter}

    asyncio.run(reconcile(adapter, pipe))

    # Capture ids up front (the sweep deletes dead ref rows on tombstone).
    ids = ids_by_basename(index)
    mem_id = ids.get("deploy-notes.md", (None, None))[0]
    daily_id = ids.get("2026-04-25.md", (None, None))[0]
    session_entry = ids.get("s1.trajectory.jsonl", (None, None))

    print("1. After initial ingest:")
    check("authored memory is searchable", bool(search_ids(search, "quokkafrazzle deploy")))
    check("daily log is searchable", bool(search_ids(search, "zlorptastic standup")))
    check("session .jsonl was ingested", session_entry[0] is not None)
    check("session classified as type=session", session_entry[1] == "session")

    # --- delete the authored memory source, run the sweep --------------------
    mem_file.unlink()
    print("\n2. After deleting the memory source file + sweep:")
    n = _sweep_deleted_sources(vault, index, adapters)
    check("sweep tombstoned exactly 1 record", n == 1)
    check("memory excluded from default search", not search_ids(search, "quokkafrazzle deploy"))
    check(
        "memory carries deleted_at in the vault",
        mem_id is not None and deleted_at_of(vault, index, mem_id) not in (None, "NO_INDEX_ROW"),
    )
    check(
        "memory recoverable via include_deleted=True",
        bool(search_ids(search, "quokkafrazzle deploy", include_deleted=True)),
    )

    # --- daily tombstoned; session NOT tombstoned ----------------------------
    daily_file.unlink()
    session_file.unlink()
    print("\n3. After deleting a daily log and a session log + sweep:")
    _sweep_deleted_sources(vault, index, adapters)
    check("daily log excluded from default search", not search_ids(search, "zlorptastic standup"))
    check(
        "daily log carries deleted_at",
        daily_id is not None
        and deleted_at_of(vault, index, daily_id) not in (None, "NO_INDEX_ROW"),
    )
    if session_entry[0] is not None:
        check(
            "session record NOT tombstoned (type guard holds)",
            deleted_at_of(vault, index, session_entry[0]) is None,
        )

    # --- restore the memory file, reconcile + sweep --------------------------
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text("# Deploy notes\n\nThe quokkafrazzle deploy uses rsync over ssh.")
    asyncio.run(reconcile(adapter, pipe))
    _sweep_deleted_sources(vault, index, adapters)
    print("\n4. After restoring the memory file + reconcile + sweep:")
    restored_id = ids_by_basename(index).get("deploy-notes.md", (None, None))[0]
    check("restored memory searchable again", bool(search_ids(search, "quokkafrazzle deploy")))
    check(
        "deleted_at cleared on restore",
        restored_id is not None and deleted_at_of(vault, index, restored_id) is None,
    )

    index.close()
    print()
    if _FAILED:
        print(f"RESULT: {_FAILED} check(s) FAILED")
        return 1
    print("RESULT: all checks passed ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
