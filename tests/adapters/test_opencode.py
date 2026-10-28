"""OpenCode capture: WAL, retry, filtering and recorded-model attribution."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.adapters.opencode import OpenCodeAdapter, read_sessions
from memstem.cli import _build_opencode_adapter
from memstem.config import Config
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Memory, Vault

Database = tuple[Path, sqlite3.Connection]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT,
                            time_created INTEGER, time_updated INTEGER);
      CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
      CREATE TABLE part (id TEXT, session_id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
      INSERT INTO session VALUES ('ses_one','/projects/example','Generated title',NULL,
                                  1793200000000,1793200001000);
    """)
    conn.commit()
    yield path, conn
    conn.close()


def msg(
    db: Database,
    mid: str,
    role: str,
    text: str,
    *,
    model: str | None = "vendor/model-a",
    provider: str | None = "openrouter",
    sid: str = "ses_one",
    **extra: Any,
) -> None:
    _, conn = db
    data = {
        "role": role,
        "providerID": provider,
        "modelID": model,
        "time": {"completed": 1793200001000},
        **extra,
    }
    n = conn.execute("select count(*) from message").fetchone()[0]
    conn.execute("insert into message values (?,?,?,?)", (mid, sid, json.dumps(data), n))
    conn.execute(
        "insert into part values (?,?,?,?,?)",
        (mid + "p", sid, mid, json.dumps({"type": "text", "text": text}), n),
    )
    conn.commit()


def read(db: Database, **kwargs: Any) -> list[MemoryRecord]:
    return read_sessions(
        db[0],
        excluded_directories=kwargs.get("excluded_directories", []),
        max_session_bytes=kwargs.get("max_session_bytes", 100000),
    )


def test_wal_models_switch_and_filtering(db: Database) -> None:
    msg(
        db,
        "u",
        "user",
        "Remember the deployed camera configuration and its exact verification steps.",
        summary={"title": "OpenCode user title", "diffs": []},
    )
    msg(db, "a", "assistant", "First response.", provider="opencode", model="big-pickle")
    msg(db, "b", "assistant", "Second response.", model="deepseek/model-b")
    msg(db, "s", "assistant", "A compacted system summary.", summary=True)
    msg(db, "i", "assistant", "Unfinished response.", time={})
    conn = db[1]
    for i, part in enumerate(
        [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "tool", "state": {"output": "raw tool payload"}},
            {"type": "text", "text": "system instructions", "synthetic": True},
            {"type": "text", "text": "ignored text", "ignored": True},
        ]
    ):
        conn.execute(
            "insert into part values (?,?,?,?,?)",
            (str(i), "ses_one", "u", json.dumps(part), i + 10),
        )
    conn.commit()
    (r,) = read(db)
    assert r.source == "opencode"
    assert "provider:opencode" in r.tags and "provider:openrouter" in r.tags
    assert "model:opencode/big-pickle" in r.tags
    assert "model:openrouter/deepseek/model-b" in r.tags
    assert "model:openrouter/vendor/model-a" not in r.tags  # requested user model is NOT evidence
    assert "**Assistant:** [opencode/big-pickle] First response." in r.body
    for excluded in [
        "private reasoning",
        "raw tool payload",
        "system instructions",
        "ignored text",
        "compacted system summary",
        "Unfinished response",
    ]:
        assert excluded not in r.body
    assert [m["message_id"] for m in r.metadata["opencode"]["messages"]] == ["a", "b"]
    assert read(db)[0] == r
    assert db[0].with_name("opencode.db-wal").exists()


def test_pipeline_idempotent_update_and_provenance(db: Database, tmp_path: Path) -> None:
    msg(
        db,
        "u",
        "user",
        "Remember which recorder was installed at the south gate and why we chose it.",
    )
    msg(
        db,
        "a",
        "assistant",
        "The south gate uses recorder Alpha because it supports the required cameras.",
    )
    vault = Vault(tmp_path / "vault")
    index = Index(tmp_path / "index.db", dimensions=768)
    index.connect()
    try:
        pipeline = Pipeline(vault, index)
        first = pipeline.process(read(db)[0])
        assert first is not None
        assert first.frontmatter.provenance is not None
        assert (
            first.frontmatter.provenance.model_dump()["opencode"]["messages"][0]["model"]
            == "vendor/model-a"
        )
        same = pipeline.process(read(db)[0])
        assert same is not None and same.id == first.id
        msg(
            db,
            "b",
            "assistant",
            "After checking firmware, recorder Beta is the replacement.",
            model="vendor/model-b",
        )
        updated = pipeline.process(read(db)[0])
        assert updated is not None and updated.id == first.id and updated.path == first.path
        from memstem.hygiene.session_distill import _extract_turn_count

        assert _extract_turn_count(updated) == 3
        stored = vault.read(updated.path)
        assert "model:openrouter/vendor/model-b" in stored.frontmatter.tags
        assert stored.frontmatter.provenance is not None
        assert len(stored.frontmatter.provenance.model_dump()["opencode"]["messages"]) == 2
        assert len(list((tmp_path / "vault" / "sessions").glob("*.md"))) == 1
    finally:
        index.close()


def test_missing_oversize_children_and_probe_exclusion(db: Database, tmp_path: Path) -> None:
    assert (
        read_sessions(tmp_path / "missing.db", excluded_directories=[], max_session_bytes=1024)
        == []
    )
    assert not (tmp_path / "missing.db").exists()
    msg(db, "u", "user", "A real user question that is useful to keep in memory.")
    assert read(db, max_session_bytes=1) == []
    assert read(db, excluded_directories=[Path("/projects")]) == []
    assert len(read(db, excluded_directories=[Path("/projects/exam")])) == 1
    db[1].execute("update session set parent_id='parent'")
    db[1].commit()
    assert read(db) == []


async def test_watch_retries_after_schema_failure_and_stops_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    sqlite3.connect(path).close()
    adapter = OpenCodeAdapter(database=path, poll_seconds=0.01)
    assert [r async for r in adapter.reconcile([])] == []
    assert not adapter._healthy
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT,
                            time_created INTEGER, time_updated INTEGER);
      CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
      CREATE TABLE part (id TEXT, session_id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
      INSERT INTO session VALUES ('one','/project','Title',NULL,1793200000000,1793200001000);
    """)
    msg(
        (path, conn),
        "u",
        "user",
        "Persistent user request after a temporary schema failure.",
        sid="one",
    )
    watch = adapter.watch([])
    r = await asyncio.wait_for(anext(watch), 1)
    assert r.source == "opencode" and adapter.watcher_alive() is True
    # No cursor: the next poll retries even if downstream failed to store this record.
    again = await asyncio.wait_for(anext(watch), 1)
    assert again.ref == r.ref
    await watch.aclose()
    assert adapter.watcher_alive() is None
    conn.close()


def test_malformed_retains_prior_and_unknown_model_not_guessed(db: Database) -> None:
    msg(db, "u", "user", "Keep this substantive user request for the inventory project.")
    msg(db, "a", "assistant", "Answer with unknown model.", provider=None, model=None)
    assert "model unknown" in read(db)[0].body
    assert not any(t.startswith("model:") for t in read(db)[0].tags)
    db[1].execute("update message set data='malformed' where id='a'")
    db[1].commit()
    assert read(db) == []


def test_opt_in_config_and_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(vault_path=tmp_path / "vault")
    assert _build_opencode_adapter(cfg) is None
    cfg.adapters.opencode.enabled = True
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    adapter = _build_opencode_adapter(cfg)
    assert adapter is not None
    assert adapter.database == tmp_path / "data/opencode/opencode.db"


def test_model_tags_are_not_project_names() -> None:
    from memstem.hygiene.project_records import _is_project_tag

    assert not _is_project_tag("model:openrouter/vendor/model")
    assert not _is_project_tag("provider:openrouter")
    assert _is_project_tag("home-ubuntu-example")


def test_malformed_session_header_does_not_block_valid_sessions(db: Database) -> None:
    msg(db, "u", "user", "Preserve this user conversation even when another session is malformed.")
    db[1].execute("INSERT INTO session VALUES ('bad',NULL,'bad',NULL,1,1)")
    db[1].commit()
    records = read(db)
    assert len(records) == 1 and records[0].metadata["opencode"]["session_id"] == "ses_one"


async def test_poll_skips_successful_writes_but_retries_failure(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memstem.cli import _drain_into_pipeline

    msg(
        db,
        "u",
        "user",
        "Remember the warehouse camera replacement decision for the next technician.",
    )
    msg(
        db,
        "a",
        "assistant",
        "Use the approved recorder and verify its firmware before installation.",
    )
    record = read(db)[0]
    vault = Vault(tmp_path / "vault")
    index = Index(tmp_path / "index.db", dimensions=768)
    index.connect()
    try:
        pipeline = Pipeline(vault, index)
        real_process = pipeline.process
        attempts = []

        def flaky(r: MemoryRecord) -> Memory | None:
            attempts.append(r.ref)
            if len(attempts) == 1:
                raise OSError("simulated disk failure")
            return real_process(r)

        monkeypatch.setattr(pipeline, "process", flaky)

        async def records() -> AsyncGenerator[MemoryRecord, None]:
            for _ in range(3):
                yield record

        await _drain_into_pipeline(pipeline, records(), skip_unchanged=True)
        assert len(attempts) == 2
        assert len(index.query_fts('"warehouse camera"', types=["session"])) == 1
    finally:
        index.close()
