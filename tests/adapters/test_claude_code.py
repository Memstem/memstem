"""Tests for the Claude Code session JSONL adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from _pytest.monkeypatch import MonkeyPatch

from memstem.adapters.base import MemoryRecord
from memstem.adapters.claude_code import (
    ClaudeCodeAdapter,
    _extract_text,
    _format_turn,
    _parse_session_file,
    _session_to_record,
)


def _write_session(
    path: Path,
    *,
    session_id: str = "abc12345-0000-0000-0000-000000000000",
    user_text: str | None = "what is 2+2",
    assistant_text: str | None = "It's 4.",
    ai_title: str | None = None,
    extra_lines: list[dict[str, Any]] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[dict[str, Any]] = []
    if user_text is not None:
        lines.append(
            {
                "type": "user",
                "uuid": "u-1",
                "timestamp": "2026-04-25T15:00:00.000Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": user_text},
            }
        )
    if assistant_text is not None:
        lines.append(
            {
                "type": "assistant",
                "uuid": "a-1",
                "timestamp": "2026-04-25T15:00:01.000Z",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": assistant_text},
            }
        )
    if ai_title is not None:
        lines.append(
            {
                "type": "ai-title",
                "timestamp": "2026-04-25T15:00:02.000Z",
                "sessionId": session_id,
                "title": ai_title,
            }
        )
    if extra_lines:
        lines.extend(extra_lines)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


async def _drain(stream: AsyncGenerator[MemoryRecord, None]) -> list[MemoryRecord]:
    return [r async for r in stream]


class TestExtractText:
    def test_string_content(self) -> None:
        assert _extract_text("hello") == "hello"

    def test_list_with_text_block(self) -> None:
        content = [{"type": "text", "text": "hi there"}]
        assert _extract_text(content) == "hi there"

    def test_summarizes_tool_use(self) -> None:
        content = [
            {"type": "text", "text": "I'll grep."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "grep ..."}},
        ]
        assert _extract_text(content) == "I'll grep.\n[tool_use: Bash]"

    def test_summarizes_tool_result(self) -> None:
        content = [{"type": "tool_result", "content": "long output blob"}]
        assert _extract_text(content) == "[tool_result]"

    def test_unknown_block_dropped(self) -> None:
        content = [{"type": "weird"}]
        assert _extract_text(content) == ""

    def test_invalid_input_returns_empty(self) -> None:
        assert _extract_text(None) == ""
        assert _extract_text(42) == ""


class TestFormatTurn:
    def test_basic(self) -> None:
        assert _format_turn("user", "hello") == "**User:** hello"
        assert _format_turn("assistant", "hi") == "**Assistant:** hi"

    def test_empty_skipped(self) -> None:
        assert _format_turn("user", "") == ""
        assert _format_turn("assistant", "   \n") == ""


class TestParseSessionFile:
    def test_basic_session(self, tmp_path: Path) -> None:
        path = _write_session(tmp_path / "session.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["session_id"] == "abc12345-0000-0000-0000-000000000000"
        assert "**User:** what is 2+2" in parsed["body"]
        assert "**Assistant:** It's 4." in parsed["body"]
        assert parsed["turn_count"] == 2
        assert parsed["first_timestamp"] == "2026-04-25T15:00:00.000Z"
        assert parsed["last_timestamp"] == "2026-04-25T15:00:01.000Z"

    def test_uses_ai_title_when_present(self, tmp_path: Path) -> None:
        path = _write_session(tmp_path / "session.jsonl", ai_title="Math Question")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["title"] == "Math Question"

    def test_falls_back_to_first_user_truncated(self, tmp_path: Path) -> None:
        long_q = "what " * 50
        path = _write_session(tmp_path / "session.jsonl", user_text=long_q)
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert len(parsed["title"]) <= 80

    def test_falls_back_to_session_id_when_no_user_turn(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "session.jsonl",
            user_text=None,
            assistant_text="hi alone",
        )
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["title"].startswith("session ")

    def test_skips_invalid_lines(self, tmp_path: Path) -> None:
        # Write valid lines plus a malformed one
        _write_session(tmp_path / "session.jsonl")
        with (tmp_path / "session.jsonl").open("a", encoding="utf-8") as f:
            f.write("not valid json\n")
        parsed = _parse_session_file(tmp_path / "session.jsonl")
        assert parsed is not None
        assert parsed["turn_count"] == 2

    def test_skips_irrelevant_types(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "session.jsonl",
            extra_lines=[
                {
                    "type": "queue-operation",
                    "timestamp": "2026-04-25T15:00:03.000Z",
                    "sessionId": "abc12345-0000-0000-0000-000000000000",
                },
                {
                    "type": "skill_listing",
                    "timestamp": "2026-04-25T15:00:04.000Z",
                },
            ],
        )
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["turn_count"] == 2

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _parse_session_file(tmp_path / "missing.jsonl") is None


class TestSessionToRecord:
    def test_basic(self, tmp_path: Path) -> None:
        path = _write_session(tmp_path / "proj/session.jsonl")
        record = _session_to_record(path)
        assert record is not None
        assert record.source == "claude-code"
        assert record.ref == str(path)
        assert record.metadata["type"] == "session"
        assert record.metadata["turn_count"] == 2
        assert record.metadata["session_id"] == "abc12345-0000-0000-0000-000000000000"

    def test_empty_body_returns_none(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "session.jsonl",
            user_text=None,
            assistant_text=None,
        )
        record = _session_to_record(path)
        assert record is None

    def test_extracts_project_tag_from_dot_prefix(self, tmp_path: Path) -> None:
        # Real Claude Code paths look like /home/.../projects/-home-ubuntu-foo/<uuid>.jsonl
        path = _write_session(tmp_path / "-home-ubuntu-foo/session.jsonl")
        record = _session_to_record(path)
        assert record is not None
        assert "home-ubuntu-foo" in record.tags


class TestReconcile:
    async def test_yields_for_each_session(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "proj1/s1.jsonl", session_id="s1-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        _write_session(tmp_path / "proj1/s2.jsonl", session_id="s2-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        _write_session(tmp_path / "proj2/s3.jsonl", session_id="s3-cccc-cccc-cccc-cccccccccccc")

        records = await _drain(ClaudeCodeAdapter().reconcile([tmp_path]))
        assert len(records) == 3
        ids = sorted(r.metadata["session_id"] for r in records)
        assert ids[0].startswith("s1-")
        assert ids[1].startswith("s2-")
        assert ids[2].startswith("s3-")

    async def test_skips_non_jsonl(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "real.jsonl")
        (tmp_path / "ignore.txt").write_text("nope")
        records = await _drain(ClaudeCodeAdapter().reconcile([tmp_path]))
        assert len(records) == 1

    async def test_handles_empty_session(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        records = await _drain(ClaudeCodeAdapter().reconcile([tmp_path]))
        assert records == []

    async def test_skips_missing_paths(self, tmp_path: Path) -> None:
        records = await _drain(ClaudeCodeAdapter().reconcile([tmp_path / "nope"]))
        assert records == []


class TestWatch:
    async def test_picks_up_new_session(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("MEMSTEM_CLAUDE_CODE_WATCH_DEBOUNCE_SECONDS", "0")
        adapter = ClaudeCodeAdapter()
        watcher = adapter.watch([tmp_path])

        async def grab_first() -> MemoryRecord:
            return await watcher.__anext__()

        task = asyncio.create_task(grab_first())
        await asyncio.sleep(0.1)
        _write_session(tmp_path / "proj/new.jsonl", ai_title="Watched Session")
        try:
            record = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await watcher.aclose()
        assert record.title == "Watched Session"
        assert record.source == "claude-code"


class TestExtraFiles:
    def _write_md(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    async def test_reconcile_emits_extras_with_instructions_tag(self, tmp_path: Path) -> None:
        extra = self._write_md(tmp_path / ".claude" / "CLAUDE.md", "# Global Rules\n\nbody text")
        adapter = ClaudeCodeAdapter(extra_files=[extra])
        records = await _drain(adapter.reconcile([]))
        assert len(records) == 1
        assert "instructions" in records[0].tags
        assert records[0].title == "Global Rules"
        assert records[0].body == "# Global Rules\n\nbody text"
        assert records[0].source == "claude-code"
        assert records[0].metadata["type"] == "memory"

    async def test_reconcile_emits_sessions_and_extras_together(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "proj/s.jsonl")
        extra = self._write_md(tmp_path / ".claude" / "CLAUDE.md", "# Rules\nbody")
        adapter = ClaudeCodeAdapter(extra_files=[extra])
        records = await _drain(adapter.reconcile([tmp_path]))
        # 1 session + 1 instructions
        assert len(records) == 2
        types = {r.metadata.get("type") for r in records}
        assert types == {"session", "memory"}

    async def test_reconcile_skips_missing_extras(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter(extra_files=[tmp_path / ".claude" / "missing.md"])
        records = await _drain(adapter.reconcile([]))
        assert records == []

    async def test_extras_use_filename_when_no_h1(self, tmp_path: Path) -> None:
        extra = self._write_md(tmp_path / ".claude" / "CLAUDE.md", "no heading here")
        adapter = ClaudeCodeAdapter(extra_files=[extra])
        records = await _drain(adapter.reconcile([]))
        assert len(records) == 1
        assert records[0].title == "CLAUDE"

    async def test_watch_picks_up_extra_change(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMSTEM_CLAUDE_CODE_WATCH_DEBOUNCE_SECONDS", "0")
        extras_dir = tmp_path / ".claude"
        extras_dir.mkdir()
        extra = extras_dir / "CLAUDE.md"
        # Create the file BEFORE starting watch so the parent dir exists.
        extra.write_text("# Initial\n", encoding="utf-8")
        adapter = ClaudeCodeAdapter(extra_files=[extra])
        watcher = adapter.watch([])

        async def grab_first() -> MemoryRecord:
            return await watcher.__anext__()

        task = asyncio.create_task(grab_first())
        await asyncio.sleep(0.1)
        extra.write_text("# Updated rule\n\nbody", encoding="utf-8")
        try:
            record = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await watcher.aclose()
        assert "instructions" in record.tags
        assert record.title == "Updated rule"

    async def test_constructor_resolves_extra_paths(self, tmp_path: Path) -> None:
        # Pass an unresolved (but valid) Path; the adapter should resolve it.
        extra = self._write_md(tmp_path / ".claude" / "CLAUDE.md", "# x\n")
        adapter = ClaudeCodeAdapter(extra_files=[extra])
        # Constructor stored it as the resolved version.
        assert all(p.is_absolute() for p in adapter.extra_files)
