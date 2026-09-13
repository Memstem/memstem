from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from memstem.adapters.openclaw import OpenClawAdapter, _trajectory_to_record
from memstem.adapters.openclaw_sqlite import discover_databases, read_database
from memstem.adapters.trajectory import merge_transcripts
from memstem.config import OpenClawLayout, OpenClawWorkspace
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault


def event(seq: int, text: str, sid: str = "session-one") -> str:
    return json.dumps(
        {
            "seq": seq,
            "sessionId": sid,
            "ts": f"2026-09-13T10:{seq % 60:02}:00Z",
            "type": "prompt.submitted",
            "data": {"prompt": text},
        }
    )


def database(root: Path, agent: str = "arbitrary-agent") -> tuple[Path, sqlite3.Connection]:
    path = root / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute(
        "CREATE TABLE trajectory_runtime_events(session_id TEXT, seq INTEGER, event_json TEXT, PRIMARY KEY(session_id, seq))"
    )
    return path, db


def put(db: sqlite3.Connection, seq: int, text: str, sid: str = "session-one") -> None:
    db.execute(
        "INSERT OR REPLACE INTO trajectory_runtime_events VALUES(?,?,?)",
        (sid, seq, event(seq, text, sid)),
    )
    db.commit()


def workspace(root: Path) -> OpenClawWorkspace:
    return OpenClawWorkspace(
        path=root,
        tag="ari",
        layout=OpenClawLayout(trajectory_sqlite_roots=[Path(".")], trajectory_poll_seconds=0.1),
    )


def test_discovery_opt_in_agents_and_escape(tmp_path: Path) -> None:
    assert discover_databases(OpenClawWorkspace(path=tmp_path, tag="ari")) == []
    path, db = database(tmp_path)
    try:
        assert discover_databases(workspace(tmp_path)) == [path]
        outside, other = database(tmp_path.parent / (tmp_path.name + "-outside"))
        other.close()
        (tmp_path / "agents" / "escaped").symlink_to(
            outside.parent.parent, target_is_directory=True
        )
        assert discover_databases(workspace(tmp_path)) == [path]
    finally:
        db.close()


def test_wal_visible_uncommitted_invisible_and_parser_parity(tmp_path: Path) -> None:
    path, db = database(tmp_path)
    try:
        put(db, 0, "Investigate the native SQLite ingestion design carefully.")
        put(db, 1, "Keep historical conversation records across runtime upgrades.")
        db.execute(
            "INSERT INTO trajectory_runtime_events VALUES(?,?,?)",
            ("session-one", 2, event(2, "uncommitted")),
        )
        assert Path(str(path) + "-wal").stat().st_size > 0
        records = read_database(path, workspace(tmp_path))
        bridge = tmp_path / "session-one.trajectory.jsonl"
        bridge.write_text(
            event(0, "Investigate the native SQLite ingestion design carefully.")
            + "\n"
            + event(1, "Keep historical conversation records across runtime upgrades.")
        )
        legacy = _trajectory_to_record(bridge)
        assert legacy is not None
        assert records[0].body == legacy.body
        assert records[0].metadata["created"] == legacy.metadata["created"]
        assert "uncommitted" not in records[0].body
        assert db.in_transaction  # reader did not commit or touch the writer
    finally:
        db.close()


def test_retention_reset_bridge_and_archive_share_canonical_history(
    tmp_path: Path, tmp_vault: Path
) -> None:
    path, db = database(tmp_path)
    index = Index(tmp_vault / "_meta/index.db", dimensions=4)
    index.connect()
    vault = Vault(tmp_vault)
    pipeline = Pipeline(vault, index)
    try:
        put(db, 0, "Original project decision that must remain searchable.")
        first = pipeline.process(read_database(path, workspace(tmp_path))[0])
        assert first is not None
        put(db, 1, "Follow-up change to the original project decision.")
        pipeline.process(read_database(path, workspace(tmp_path))[0])
        db.execute("DELETE FROM trajectory_runtime_events")
        db.commit()
        put(db, 0, "Reset creates a new rolling window with reused sequence zero.")
        pipeline.process(read_database(path, workspace(tmp_path))[0])
        # A stale bridge or archive arriving after native must never shrink it.
        archive = tmp_path / "session-one.trajectory.jsonl"
        archive.write_text(event(0, "Original project decision that must remain searchable."))
        bridged = _trajectory_to_record(archive)
        assert bridged is not None
        pipeline.process(bridged)
        result = vault.read("sessions/session-one.md")
        assert result.id == first.id
        assert all(word in result.body for word in ("Original", "Follow-up", "Reset"))
        assert (
            index.db.execute("SELECT count(*) FROM memories WHERE type='session'").fetchone()[0]
            == 1
        )
        assert (
            index.db.execute("SELECT count(DISTINCT memory_id) FROM record_map").fetchone()[0] == 1
        )
        # Rebuildable index: recover the ID/history from canonical Markdown.
        index.db.execute("DELETE FROM record_map")
        index.db.commit()
        again = pipeline.process(bridged)
        assert again is not None and again.id == first.id and again.body == result.body
        db.execute("DELETE FROM trajectory_runtime_events")
        db.commit()
        assert read_database(path, workspace(tmp_path)) == []
        assert vault.read("sessions/session-one.md").body == result.body
    finally:
        db.close()
        index.close()


def test_gap_size_and_invalid_session_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path, db = database(tmp_path)
    try:
        put(db, 10, "Surviving conversation after upstream retention pruned earlier events.")
        put(db, 12, "Another surviving message with an internal sequence gap.")
        put(db, 0, "malicious", "../escape")
        ws = workspace(tmp_path)
        assert len(read_database(path, ws)) == 1
        assert "retention/gap" in caplog.text and "invalid OpenClaw session id" in caplog.text
        ws.layout.max_trajectory_bytes = 1
        assert read_database(path, ws) == []
        assert "oversized" in caplog.text
    finally:
        db.close()


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("A B C", "B C D", "A B C D"),
        ("A B C", "B", "A B C"),
        ("A A B", "A B C", "A A B C"),
        ("A B", "C D", "A B C D"),
    ],
)
def test_monotonic_merge(old: str, new: str, expected: str) -> None:
    def body(s: str) -> str:
        return "\n\n".join(f"**User:** {x}" for x in s.split())

    assert merge_transcripts(body(old), body(new)) == body(expected)


async def test_poll_incremental_restore_and_reconcile(tmp_path: Path) -> None:
    _path, db = database(tmp_path)
    try:
        put(db, 0, "Initial long enough conversation from an arbitrary agent.")
        adapter = OpenClawAdapter([workspace(tmp_path)])
        assert len([r async for r in adapter._poll_external()]) == 1
        assert [r async for r in adapter._poll_external()] == []
        put(db, 1, "An incremental update should trigger replay of this session.")
        assert len([r async for r in adapter._poll_external()]) == 1
        db.execute("DELETE FROM trajectory_runtime_events")
        db.commit()
        put(db, 0, "Same sequence and a new message after a restored database.")
        restored = [r async for r in adapter._poll_external()]
        assert "restored" in restored[0].body
        assert len([r async for r in adapter.reconcile([])]) == 1
        # WAL changes must be discovered even without a JSONL filesystem event.
        stream = adapter.watch([])
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.15)
        put(db, 1, "Live watcher notices this committed WAL event.")
        record = await asyncio.wait_for(task, 3)
        assert "Live watcher" in record.body
        await stream.aclose()
    finally:
        db.close()


def test_compacted_snapshots_retain_earlier_conversation() -> None:
    from memstem.adapters.openclaw import _parse_trajectory_lines

    events = []
    for seq, texts in enumerate(
        (["first decision", "second decision"], ["new window after compaction"])
    ):
        events.append(
            json.dumps(
                {
                    "seq": seq,
                    "type": "model.completed",
                    "data": {
                        "messagesSnapshot": [{"role": "user", "content": t} for t in texts],
                    },
                }
            )
        )
    parsed = _parse_trajectory_lines(events, "snapshot-session", preserve_history=True)
    assert all(
        text in parsed["body"] for text in ("first decision", "second decision", "new window")
    )
    assert parsed["body"].count("first decision") == 1
