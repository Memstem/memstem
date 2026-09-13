from __future__ import annotations

import json
from pathlib import Path

import pytest

from memstem.core import retrieval_pending as pending
from memstem.core.index import Index


def test_pending_queue_bounds_and_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = Index(tmp_path / "index.db", dimensions=4)
    index.connect()
    try:
        row = ("2026-09-13", "get", None, "cli", "deleted-memory", None, None)
        assert pending.enqueue(index.db, [row])
        root = pending.directory(index.db)
        assert root is not None
        path = next(root.glob("*.json"))
        assert path.stat().st_mode & 0o077 == 0
        assert json.loads(path.read_text())[0][4] == "deleted-memory"
        monkeypatch.setattr(pending, "MAX_PENDING_FILES", 1)
        assert not pending.enqueue(index.db, [row])
        monkeypatch.setattr(pending, "MAX_PENDING_FILES", 1000)
        monkeypatch.setattr(pending, "MAX_PENDING_BYTES", 1)
        assert not pending.enqueue(index.db, [row])
        monkeypatch.setattr(pending, "MAX_BATCH_BYTES", 1)
        assert not pending.enqueue(index.db, [row])
    finally:
        index.close()


def test_replay_survives_restart_and_deleted_memory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "index.db"
    original = Index(path, dimensions=4)
    original.connect()
    assert pending.enqueue(original.db, [("2026-09-13", "get", None, "cli", "deleted", None, None)])
    original.close()
    restarted = Index(path, dimensions=4)
    restarted.connect()
    try:
        with restarted.db:
            files = pending.replay(restarted.db)
        assert len(files) == 1
        pending.remove_replayed(files)
        assert not files[0].exists()
        assert "failed" not in caplog.text
    finally:
        restarted.close()


def test_bad_pending_json_is_reported_and_retained(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = Index(tmp_path / "index.db", dimensions=4)
    index.connect()
    try:
        root = pending.directory(index.db)
        assert root is not None
        root.mkdir()
        malformed = root / "bad.json"
        malformed.write_text("not JSON")
        assert pending.replay(index.db) == []
        assert malformed.exists()
        assert "pending replay failed" in caplog.text
    finally:
        index.close()
