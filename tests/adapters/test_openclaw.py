"""Tests for the OpenClaw filesystem adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from memstem.adapters.base import MemoryRecord
from memstem.adapters.openclaw import (
    OpenClawAdapter,
    _classify_trajectory_path,
    _classify_type,
    _classify_workspace_path,
    _extract_h1,
    _file_to_record,
    _parse_trajectory_file,
    _trajectory_to_record,
)
from memstem.config import OpenClawLayout, OpenClawWorkspace


def _write(file: Path, content: str) -> Path:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    return file


async def _drain(stream: AsyncIterator[MemoryRecord]) -> list[MemoryRecord]:
    return [r async for r in stream]


class TestClassifyType:
    def test_skill_files(self, tmp_path: Path) -> None:
        assert _classify_type(tmp_path / "skills/email/SKILL.md") == "skill"

    def test_daily_files(self, tmp_path: Path) -> None:
        assert _classify_type(tmp_path / "memory/2026-04-25.md") == "daily"

    def test_memory_default(self, tmp_path: Path) -> None:
        assert _classify_type(tmp_path / "memory/people.md") == "memory"
        assert _classify_type(tmp_path / "MEMORY.md") == "memory"

    def test_almost_daily_filenames_dont_match(self, tmp_path: Path) -> None:
        # Has trailing junk → not a date.
        assert _classify_type(tmp_path / "2026-04-25-deploy.md") == "memory"


class TestExtractH1:
    def test_finds_first_h1(self) -> None:
        body = "# Title\n\nbody text"
        assert _extract_h1(body) == "Title"

    def test_returns_none_when_missing(self) -> None:
        assert _extract_h1("just body, no heading") is None

    def test_ignores_h2(self) -> None:
        body = "## Section\n\nbody"
        assert _extract_h1(body) is None

    def test_returns_first_when_multiple(self) -> None:
        body = "# First\n\nstuff\n\n# Second\n"
        assert _extract_h1(body) == "First"


class TestFileToRecord:
    def test_plain_markdown_uses_h1_as_title(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "people.md", "# People\n\nBrad runs TechPro.\n")
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert record.title == "People"
        assert record.body == "# People\n\nBrad runs TechPro."
        assert record.metadata["type"] == "memory"
        assert record.source == "openclaw"
        assert record.ref == str(path)

    def test_falls_back_to_stem_when_no_h1(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "no-heading.md", "just some content")
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert record.title == "no-heading"

    def test_uses_frontmatter_title_when_present(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "with-fm.md",
            "---\ntitle: Frontmatter Title\ntags: [a, b]\n---\n\n# Body H1\n\nbody",
        )
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert record.title == "Frontmatter Title"
        assert record.tags == ["a", "b"]

    def test_skill_file_classified(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "skills/x/SKILL.md", "# X Skill\n")
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert record.metadata["type"] == "skill"

    def test_daily_file_classified(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "memory/2026-04-25.md", "# 2026-04-25\n\nlog")
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert record.metadata["type"] == "daily"

    def test_unreadable_file_returns_none(self, tmp_path: Path) -> None:
        # Path doesn't exist
        record = _file_to_record(tmp_path / "missing.md", "openclaw")
        assert record is None

    def test_metadata_records_mtime(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "ts.md", "body")
        record = _file_to_record(path, "openclaw")
        assert record is not None
        assert "T" in record.metadata["created"]
        assert "T" in record.metadata["updated"]


class TestReconcile:
    async def test_yields_all_markdown_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "memory/a.md", "# A")
        _write(tmp_path / "memory/b.md", "# B")
        _write(tmp_path / "skills/s1/SKILL.md", "# S1")
        _write(tmp_path / "memory/notes.txt", "ignored, wrong suffix")

        records = await _drain(OpenClawAdapter().reconcile([tmp_path]))
        titles = sorted(r.title or "" for r in records)
        assert titles == ["A", "B", "S1"]

    async def test_classifies_each_record(self, tmp_path: Path) -> None:
        _write(tmp_path / "memory/people.md", "# People")
        _write(tmp_path / "memory/2026-04-25.md", "# Daily")
        _write(tmp_path / "skills/email/SKILL.md", "# Email Skill")

        records = await _drain(OpenClawAdapter().reconcile([tmp_path]))
        types = sorted(r.metadata["type"] for r in records)
        assert types == ["daily", "memory", "skill"]

    async def test_skips_missing_paths(self, tmp_path: Path) -> None:
        records = await _drain(OpenClawAdapter().reconcile([tmp_path / "nope"]))
        assert records == []

    async def test_handles_explicit_file_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "single.md", "# Single")
        records = await _drain(OpenClawAdapter().reconcile([path]))
        assert len(records) == 1
        assert records[0].title == "Single"


class TestWatch:
    async def test_picks_up_new_file(self, tmp_path: Path) -> None:
        adapter = OpenClawAdapter()
        watcher = adapter.watch([tmp_path])

        async def grab_first() -> MemoryRecord:
            return await watcher.__anext__()

        task = asyncio.create_task(grab_first())
        # Give watchdog a beat to register the inotify watch before writing.
        await asyncio.sleep(0.1)
        _write(tmp_path / "new.md", "# Watched\n\nbody")
        try:
            record = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await watcher.aclose()
        assert record.title == "Watched"
        assert record.source == "openclaw"


class TestClassifyWorkspacePath:
    def test_memory_md(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari").mkdir()
        interesting, extra = _classify_workspace_path(tmp_path / "ari" / "MEMORY.md", ws)
        assert interesting is True
        assert extra == ["core"]

    def test_claude_md(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari").mkdir()
        interesting, extra = _classify_workspace_path(tmp_path / "ari" / "CLAUDE.md", ws)
        assert interesting is True
        assert extra == ["instructions"]

    def test_memory_subdir_file(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari" / "memory").mkdir(parents=True)
        interesting, extra = _classify_workspace_path(tmp_path / "ari" / "memory" / "people.md", ws)
        assert interesting is True
        assert extra == []

    def test_skill_md(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari" / "skills" / "deploy").mkdir(parents=True)
        interesting, extra = _classify_workspace_path(
            tmp_path / "ari" / "skills" / "deploy" / "SKILL.md", ws
        )
        assert interesting is True
        assert extra == []

    def test_random_file_in_workspace_ignored(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari").mkdir()
        interesting, _ = _classify_workspace_path(tmp_path / "ari" / "openclaw.json", ws)
        assert interesting is False

    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        (tmp_path / "ari").mkdir()
        interesting, _ = _classify_workspace_path(tmp_path / "elsewhere.md", ws)
        assert interesting is False


class TestWorkspaceMode:
    def _seed_workspace(self, root: Path, agent: str) -> Path:
        ws_root = root / agent
        _write(ws_root / "MEMORY.md", "# Core for " + agent)
        _write(ws_root / "CLAUDE.md", "# How " + agent + " operates")
        _write(ws_root / "memory" / "people.md", "# People\n\nBrad")
        _write(ws_root / "memory" / "2026-04-25.md", "# Daily 2026-04-25\n\nlog")
        _write(ws_root / "skills" / "deploy" / "SKILL.md", "# Deploy skill")
        return ws_root

    async def test_emits_records_per_workspace_with_agent_tag(self, tmp_path: Path) -> None:
        ari_root = self._seed_workspace(tmp_path, "ari")
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ari_root, tag="ari")])
        records = await _drain(adapter.reconcile([]))
        # 5 files: MEMORY.md, CLAUDE.md, 2 memory/, 1 skill
        assert len(records) == 5
        for r in records:
            assert "agent:ari" in r.tags

    async def test_memory_md_gets_core_tag(self, tmp_path: Path) -> None:
        ari_root = self._seed_workspace(tmp_path, "ari")
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ari_root, tag="ari")])
        records = await _drain(adapter.reconcile([]))
        memory_md = next(r for r in records if r.ref.endswith("MEMORY.md"))
        assert "core" in memory_md.tags
        assert "agent:ari" in memory_md.tags

    async def test_claude_md_gets_instructions_tag(self, tmp_path: Path) -> None:
        ari_root = self._seed_workspace(tmp_path, "ari")
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ari_root, tag="ari")])
        records = await _drain(adapter.reconcile([]))
        claude_md = next(r for r in records if r.ref.endswith("CLAUDE.md"))
        assert "instructions" in claude_md.tags

    async def test_multi_agent_records_tagged_distinctly(self, tmp_path: Path) -> None:
        self._seed_workspace(tmp_path, "ari")
        self._seed_workspace(tmp_path, "sarah")
        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(path=tmp_path / "ari", tag="ari"),
                OpenClawWorkspace(path=tmp_path / "sarah", tag="sarah"),
            ]
        )
        records = await _drain(adapter.reconcile([]))
        ari_only = [r for r in records if "agent:ari" in r.tags]
        sarah_only = [r for r in records if "agent:sarah" in r.tags]
        assert len(ari_only) == 5
        assert len(sarah_only) == 5
        # No record should carry both agent tags.
        for r in records:
            agent_tags = [t for t in r.tags if t.startswith("agent:")]
            assert len(agent_tags) == 1

    async def test_shared_files_get_shared_tag(self, tmp_path: Path) -> None:
        rules = _write(tmp_path / "HARD-RULES.md", "# Hard Rules\n\ncontent")
        adapter = OpenClawAdapter(shared_files=[rules])
        records = await _drain(adapter.reconcile([]))
        assert len(records) == 1
        assert "shared" in records[0].tags
        assert not any(t.startswith("agent:") for t in records[0].tags)

    async def test_missing_shared_file_silently_skipped(self, tmp_path: Path) -> None:
        adapter = OpenClawAdapter(shared_files=[tmp_path / "missing.md"])
        records = await _drain(adapter.reconcile([]))
        assert records == []

    async def test_missing_workspace_silently_skipped(self, tmp_path: Path) -> None:
        adapter = OpenClawAdapter(
            workspaces=[OpenClawWorkspace(path=tmp_path / "nonexistent", tag="ghost")]
        )
        records = await _drain(adapter.reconcile([]))
        assert records == []

    async def test_legacy_mode_still_works(self, tmp_path: Path) -> None:
        # No workspace config → reconcile should walk the path argument.
        _write(tmp_path / "memory" / "a.md", "# A")
        records = await _drain(OpenClawAdapter().reconcile([tmp_path]))
        assert len(records) == 1
        assert "agent:" not in " ".join(records[0].tags or [""])


class TestWorkspaceLayout:
    """Per-workspace layout overrides for non-canonical OpenClaw setups."""

    async def test_custom_memory_dir(self, tmp_path: Path) -> None:
        # Workspace keeps memories under `notes/`, not `memory/`.
        ws_root = tmp_path / "custom"
        _write(ws_root / "MEMORY.md", "# core")
        _write(ws_root / "notes" / "fact.md", "# Fact")
        # Decoy under the default memory/ dir — should NOT be picked up.
        _write(ws_root / "memory" / "should-be-skipped.md", "# decoy")

        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(
                    path=ws_root,
                    tag="custom",
                    layout=OpenClawLayout(memory_dirs=["notes"]),
                )
            ]
        )
        records = await _drain(adapter.reconcile([]))
        refs = {r.ref for r in records}
        assert str(ws_root / "notes" / "fact.md") in refs
        assert str(ws_root / "memory" / "should-be-skipped.md") not in refs

    async def test_skip_memory_md(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "custom"
        _write(ws_root / "MEMORY.md", "# would normally be ingested")
        _write(ws_root / "memory" / "ok.md", "# ok")

        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(
                    path=ws_root,
                    tag="custom",
                    layout=OpenClawLayout(memory_md=None),
                )
            ]
        )
        records = await _drain(adapter.reconcile([]))
        refs = {r.ref for r in records}
        assert str(ws_root / "MEMORY.md") not in refs
        assert str(ws_root / "memory" / "ok.md") in refs

    async def test_skip_skills(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "custom"
        _write(ws_root / "MEMORY.md", "# core")
        _write(ws_root / "skills" / "deploy" / "SKILL.md", "# skill")

        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(
                    path=ws_root,
                    tag="custom",
                    layout=OpenClawLayout(skills_dirs=[]),
                )
            ]
        )
        records = await _drain(adapter.reconcile([]))
        assert all(not r.ref.endswith("SKILL.md") for r in records)

    async def test_multiple_memory_dirs(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "custom"
        _write(ws_root / "memory" / "a.md", "# A")
        _write(ws_root / "notes" / "b.md", "# B")
        _write(ws_root / "logs" / "c.md", "# C")

        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(
                    path=ws_root,
                    tag="custom",
                    layout=OpenClawLayout(memory_dirs=["memory", "notes", "logs"]),
                )
            ]
        )
        records = await _drain(adapter.reconcile([]))
        refs = {r.ref for r in records}
        assert str(ws_root / "memory" / "a.md") in refs
        assert str(ws_root / "notes" / "b.md") in refs
        assert str(ws_root / "logs" / "c.md") in refs

    async def test_default_layout_unchanged(self, tmp_path: Path) -> None:
        # Constructing OpenClawWorkspace without `layout=` should preserve
        # the canonical behavior — important for backwards compat.
        ws_root = tmp_path / "ari"
        _write(ws_root / "MEMORY.md", "# core")
        _write(ws_root / "CLAUDE.md", "# rules")
        _write(ws_root / "memory" / "people.md", "# people")
        _write(ws_root / "skills" / "x" / "SKILL.md", "# skill")

        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ws_root, tag="ari")])
        records = await _drain(adapter.reconcile([]))
        assert len(records) == 4

    def test_classify_path_honors_custom_memory_dir(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(
            path=tmp_path / "x",
            tag="x",
            layout=OpenClawLayout(memory_dirs=["notes"]),
        )
        (tmp_path / "x" / "notes").mkdir(parents=True)
        notes_file = tmp_path / "x" / "notes" / "a.md"
        notes_file.write_text("# A")
        decoy = tmp_path / "x" / "memory" / "b.md"
        decoy.parent.mkdir(parents=True)
        decoy.write_text("# B")

        is_int, _ = _classify_workspace_path(notes_file, ws)
        assert is_int is True
        is_int, _ = _classify_workspace_path(decoy, ws)
        assert is_int is False


class TestTrajectoryParser:
    """Parse OpenClaw `*.trajectory.jsonl` files into session records."""

    @staticmethod
    def _write_trajectory(path: Path, events: list[dict[str, Any]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return path

    def test_parses_user_and_assistant_turns(self, tmp_path: Path) -> None:
        traj = self._write_trajectory(
            tmp_path / "abc123.trajectory.jsonl",
            [
                {
                    "type": "session.started",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "sessionId": "abc123",
                    "workspaceDir": "/home/ubuntu/ari",
                    "data": {"agentId": "main"},
                },
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"prompt": "Hello, can you help?"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:02.000Z",
                    "data": {"assistantTexts": ["Sure — what do you need?"]},
                },
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:05.000Z",
                    "data": {"prompt": "Test the parser."},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:06.000Z",
                    "data": {"assistantTexts": ["Done."]},
                },
                {"type": "session.ended", "ts": "2026-04-26T23:00:07.000Z", "data": {}},
            ],
        )
        parsed = _parse_trajectory_file(traj)
        assert parsed is not None
        assert parsed["session_id"] == "abc123"
        assert parsed["turn_count"] == 4
        assert "**User:** Hello, can you help?" in parsed["body"]
        assert "**Assistant:** Sure — what do you need?" in parsed["body"]
        assert "**User:** Test the parser." in parsed["body"]
        assert "**Assistant:** Done." in parsed["body"]
        assert parsed["title"] == "Hello, can you help?"
        assert parsed["first_timestamp"] == "2026-04-26T23:00:00.000Z"
        assert parsed["last_timestamp"] == "2026-04-26T23:00:07.000Z"
        assert parsed["workspace_dir"] == "/home/ubuntu/ari"
        assert parsed["agent_id"] == "main"

    def test_skips_operational_events(self, tmp_path: Path) -> None:
        # context.compiled, trace.artifacts, openclaw, etc. should not contribute turns.
        traj = self._write_trajectory(
            tmp_path / "x.trajectory.jsonl",
            [
                {
                    "type": "context.compiled",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "data": {"systemPrompt": "long system prompt content"},
                },
                {
                    "type": "trace.artifacts",
                    "ts": "2026-04-26T23:00:00.001Z",
                    "data": {"items": ["a", "b", "c"]},
                },
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"prompt": "real user turn"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:02.000Z",
                    "data": {"assistantTexts": ["real assistant turn"]},
                },
            ],
        )
        parsed = _parse_trajectory_file(traj)
        assert parsed is not None
        assert parsed["turn_count"] == 2

    def test_empty_assistant_texts_skipped(self, tmp_path: Path) -> None:
        traj = self._write_trajectory(
            tmp_path / "x.trajectory.jsonl",
            [
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "data": {"prompt": "hi"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"assistantTexts": []},
                },
            ],
        )
        parsed = _parse_trajectory_file(traj)
        assert parsed is not None
        assert parsed["turn_count"] == 1

    def test_session_id_falls_back_to_filename(self, tmp_path: Path) -> None:
        # No sessionId/traceId in any event → derive from filename stem.
        traj = self._write_trajectory(
            tmp_path / "deadbeef.trajectory.jsonl",
            [
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "data": {"prompt": "hi"},
                },
            ],
        )
        parsed = _parse_trajectory_file(traj)
        assert parsed is not None
        assert parsed["session_id"] == "deadbeef"

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        traj = tmp_path / "x.trajectory.jsonl"
        traj.write_text(
            "not json\n"
            '{"type":"prompt.submitted","ts":"2026-04-26T23:00:00.000Z","data":{"prompt":"hi"}}\n'
            "{ bad json\n"
            '{"type":"model.completed","ts":"2026-04-26T23:00:01.000Z","data":{"assistantTexts":["ok"]}}\n',
            encoding="utf-8",
        )
        parsed = _parse_trajectory_file(traj)
        assert parsed is not None
        assert parsed["turn_count"] == 2

    def test_record_carries_session_metadata(self, tmp_path: Path) -> None:
        traj = self._write_trajectory(
            tmp_path / "abc.trajectory.jsonl",
            [
                {
                    "type": "session.started",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "sessionId": "abc-session",
                    "workspaceDir": "/x",
                    "data": {"agentId": "main"},
                },
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"prompt": "first prompt"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:02.000Z",
                    "data": {"assistantTexts": ["resp"]},
                },
            ],
        )
        record = _trajectory_to_record(traj)
        assert record is not None
        assert record.metadata["type"] == "session"
        assert record.metadata["session_id"] == "abc-session"
        assert record.metadata["turn_count"] == 2
        assert record.metadata["workspace_dir"] == "/x"
        assert record.metadata["agent_id"] == "main"
        assert record.title == "first prompt"

    def test_empty_trajectory_returns_none(self, tmp_path: Path) -> None:
        traj = tmp_path / "x.trajectory.jsonl"
        traj.write_text("", encoding="utf-8")
        record = _trajectory_to_record(traj)
        assert record is None

    def test_only_operational_events_returns_none(self, tmp_path: Path) -> None:
        # No prompt.submitted or model.completed → no turns → no record.
        traj = self._write_trajectory(
            tmp_path / "x.trajectory.jsonl",
            [
                {"type": "session.started", "ts": "2026-04-26T23:00:00.000Z"},
                {"type": "session.ended", "ts": "2026-04-26T23:00:01.000Z"},
            ],
        )
        record = _trajectory_to_record(traj)
        assert record is None


class TestTrajectoryClassification:
    def test_classifies_trajectory_inside_session_dir(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(
            path=tmp_path / "ari",
            tag="ari",
            layout=OpenClawLayout(session_dirs=["agents/main/sessions"]),
        )
        traj = tmp_path / "ari" / "agents" / "main" / "sessions" / "x.trajectory.jsonl"
        traj.parent.mkdir(parents=True)
        traj.write_text("")
        assert _classify_trajectory_path(traj, ws) is True

    def test_rejects_md_file(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(
            path=tmp_path / "ari",
            tag="ari",
            layout=OpenClawLayout(session_dirs=["agents/main/sessions"]),
        )
        md = tmp_path / "ari" / "agents" / "main" / "sessions" / "note.md"
        md.parent.mkdir(parents=True)
        md.write_text("# note")
        assert _classify_trajectory_path(md, ws) is False

    def test_rejects_trajectory_outside_session_dirs(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(
            path=tmp_path / "ari",
            tag="ari",
            layout=OpenClawLayout(session_dirs=["agents/main/sessions"]),
        )
        # Trajectory file but in a different directory than configured.
        traj = tmp_path / "ari" / "elsewhere" / "x.trajectory.jsonl"
        traj.parent.mkdir(parents=True)
        traj.write_text("")
        assert _classify_trajectory_path(traj, ws) is False

    def test_default_session_dirs_empty_means_no_match(self, tmp_path: Path) -> None:
        ws = OpenClawWorkspace(path=tmp_path / "ari", tag="ari")
        traj = tmp_path / "ari" / "agents" / "main" / "sessions" / "x.trajectory.jsonl"
        traj.parent.mkdir(parents=True)
        traj.write_text("")
        # With default empty session_dirs, the trajectory is invisible.
        assert _classify_trajectory_path(traj, ws) is False


class TestWorkspaceTrajectoryReconcile:
    @staticmethod
    def _write_trajectory(path: Path, events: list[dict[str, Any]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return path

    async def test_emits_trajectory_records_with_agent_tag(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "ari"
        _write(ws_root / "MEMORY.md", "# core")
        self._write_trajectory(
            ws_root / "agents" / "main" / "sessions" / "abc.trajectory.jsonl",
            [
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "data": {"prompt": "hello"},
                },
                {
                    "type": "model.completed",
                    "ts": "2026-04-26T23:00:01.000Z",
                    "data": {"assistantTexts": ["hi"]},
                },
            ],
        )
        adapter = OpenClawAdapter(
            workspaces=[
                OpenClawWorkspace(
                    path=ws_root,
                    tag="ari",
                    layout=OpenClawLayout(session_dirs=["agents/main/sessions"]),
                )
            ]
        )
        records = await _drain(adapter.reconcile([]))
        # 1 MEMORY.md + 1 trajectory
        assert len(records) == 2
        traj_record = next(r for r in records if r.metadata.get("type") == "session")
        assert "agent:ari" in traj_record.tags
        assert "**User:** hello" in traj_record.body
        assert "**Assistant:** hi" in traj_record.body

    async def test_default_layout_skips_trajectories(self, tmp_path: Path) -> None:
        # Without session_dirs configured, trajectory files are not ingested
        # — even if they exist on disk.
        ws_root = tmp_path / "ari"
        _write(ws_root / "MEMORY.md", "# core")
        self._write_trajectory(
            ws_root / "agents" / "main" / "sessions" / "abc.trajectory.jsonl",
            [
                {
                    "type": "prompt.submitted",
                    "ts": "2026-04-26T23:00:00.000Z",
                    "data": {"prompt": "hello"},
                },
            ],
        )
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ws_root, tag="ari")])
        records = await _drain(adapter.reconcile([]))
        assert len(records) == 1
        assert records[0].metadata.get("type") != "session"


class TestWorkspaceWatch:
    async def test_picks_up_memory_md_change(self, tmp_path: Path) -> None:
        ari_root = tmp_path / "ari"
        ari_root.mkdir()
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ari_root, tag="ari")])
        watcher = adapter.watch([])

        async def grab_first() -> MemoryRecord:
            return await watcher.__anext__()

        task = asyncio.create_task(grab_first())
        await asyncio.sleep(0.1)
        _write(ari_root / "MEMORY.md", "# Core\n\nbody")
        try:
            record = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await watcher.aclose()
        assert "agent:ari" in record.tags
        assert "core" in record.tags

    async def test_ignores_unrelated_files_in_workspace(self, tmp_path: Path) -> None:
        ari_root = tmp_path / "ari"
        ari_root.mkdir()
        adapter = OpenClawAdapter(workspaces=[OpenClawWorkspace(path=ari_root, tag="ari")])
        watcher = adapter.watch([])

        async def grab_first() -> MemoryRecord:
            return await watcher.__anext__()

        task = asyncio.create_task(grab_first())
        await asyncio.sleep(0.1)
        # Write an unrelated file first, then a real one. Only the real one should arrive.
        _write(ari_root / "notes.txt", "irrelevant")  # not .md
        _write(ari_root / "openclaw.json", "{}")
        await asyncio.sleep(0.1)
        _write(ari_root / "MEMORY.md", "# Core")
        try:
            record = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await watcher.aclose()
        # The first record we receive should be MEMORY.md.
        assert record.ref.endswith("MEMORY.md")
