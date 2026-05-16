"""Tests for the Codex session / skill / memory adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from memstem.adapters.base import MemoryRecord
from memstem.adapters.codex import (
    CodexAdapter,
    _extract_message_text,
    _is_user_skill_path,
    _markdown_to_record,
    _parse_session_file,
    _session_to_record,
    _slugify_cwd,
)


def _session_meta_line(
    session_id: str = "abc12345-0000-0000-0000-000000000000",
    cwd: str = "/home/ubuntu/memstem",
    cli_version: str = "0.130.0",
    timestamp: str = "2026-05-16T15:00:00.000Z",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "timestamp": timestamp,
            "cwd": cwd,
            "originator": "codex-tui",
            "cli_version": cli_version,
            "source": "cli",
            "model_provider": "openai",
        },
    }


def _message_line(
    role: str,
    text: str,
    *,
    timestamp: str = "2026-05-16T15:00:01.000Z",
    block_type: str = "input_text",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }


def _function_call_line(name: str = "exec_command") -> dict[str, Any]:
    return {
        "timestamp": "2026-05-16T15:00:02.000Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": '{"cmd":"pwd"}',
            "call_id": "call_abc",
        },
    }


def _function_call_output_line() -> dict[str, Any]:
    return {
        "timestamp": "2026-05-16T15:00:03.000Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "huge output blob",
        },
    }


def _write_session(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _basic_session(path: Path, **meta_kwargs: Any) -> Path:
    return _write_session(
        path,
        [
            _session_meta_line(**meta_kwargs),
            _message_line(
                "user",
                "<environment_context>\n  <cwd>/home/ubuntu</cwd>\n</environment_context>",
            ),
            _message_line("developer", "<permissions instructions>\nbig boilerplate"),
            _message_line("user", "what is 2+2"),
            _message_line("assistant", "It's 4.", block_type="output_text"),
        ],
    )


async def _drain(stream: AsyncGenerator[MemoryRecord, None]) -> list[MemoryRecord]:
    return [r async for r in stream]


class TestSlugifyCwd:
    def test_basic(self) -> None:
        assert _slugify_cwd("/home/ubuntu/memstem") == "home-ubuntu-memstem"

    def test_root_only(self) -> None:
        assert _slugify_cwd("/") == ""

    def test_empty(self) -> None:
        assert _slugify_cwd("") == ""

    def test_special_chars_collapsed(self) -> None:
        assert _slugify_cwd("/home/ubuntu/my project (test)") == "home-ubuntu-my-project-test"


class TestExtractMessageText:
    def test_string_content(self) -> None:
        assert _extract_message_text("hello") == "hello"

    def test_input_text_block(self) -> None:
        content = [{"type": "input_text", "text": "hi"}]
        assert _extract_message_text(content) == "hi"

    def test_output_text_block(self) -> None:
        content = [{"type": "output_text", "text": "bye"}]
        assert _extract_message_text(content) == "bye"

    def test_unknown_block_dropped(self) -> None:
        content = [{"type": "weird", "text": "x"}]
        assert _extract_message_text(content) == ""

    def test_empty_text_skipped(self) -> None:
        content = [{"type": "input_text", "text": ""}, {"type": "input_text", "text": "y"}]
        assert _extract_message_text(content) == "y"

    def test_invalid_input(self) -> None:
        assert _extract_message_text(None) == ""
        assert _extract_message_text(42) == ""


class TestParseSessionFile:
    def test_extracts_metadata(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["session_id"] == "abc12345-0000-0000-0000-000000000000"
        assert parsed["cwd"] == "/home/ubuntu/memstem"
        assert parsed["cli_version"] == "0.130.0"
        assert parsed["model_provider"] == "openai"

    def test_drops_developer_boilerplate(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert "permissions instructions" not in parsed["body"]
        assert "**Developer:**" not in parsed["body"]

    def test_drops_env_context_user_stub(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert "<environment_context>" not in parsed["body"]

    def test_keeps_real_user_and_assistant_turns(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert "**User:** what is 2+2" in parsed["body"]
        assert "**Assistant:** It's 4." in parsed["body"]
        assert parsed["turn_count"] == 2

    def test_summarizes_function_calls(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "rollout.jsonl",
            [
                _session_meta_line(),
                _message_line("user", "list files"),
                _function_call_line("exec_command"),
                _function_call_output_line(),
                _message_line("assistant", "done", block_type="output_text"),
            ],
        )
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert "[function_call: exec_command]" in parsed["body"]
        assert "[function_call_output]" in parsed["body"]
        assert "huge output blob" not in parsed["body"]

    def test_title_taken_from_first_user_turn(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["title"] == "what is 2+2"

    def test_default_title_when_no_user_turn(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "rollout.jsonl",
            [
                _session_meta_line(session_id="deadbeef-1111-2222-3333-444444444444"),
                _message_line("assistant", "hi", block_type="output_text"),
            ],
        )
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert parsed["title"] == "codex session deadbeef"

    def test_handles_malformed_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            json.dumps(_session_meta_line())
            + "\nNOT JSON\n"
            + json.dumps(_message_line("user", "hello"))
            + "\n",
            encoding="utf-8",
        )
        parsed = _parse_session_file(path)
        assert parsed is not None
        assert "**User:** hello" in parsed["body"]

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _parse_session_file(tmp_path / "nope.jsonl") is None


class TestSessionToRecord:
    def test_produces_record(self, tmp_path: Path) -> None:
        path = _basic_session(tmp_path / "rollout.jsonl")
        record = _session_to_record(path)
        assert record is not None
        assert record.source == "codex"
        assert record.ref == str(path)
        assert record.metadata["type"] == "session"
        assert record.metadata["session_id"] == "abc12345-0000-0000-0000-000000000000"
        assert record.metadata["cwd"] == "/home/ubuntu/memstem"
        assert record.metadata["cli_version"] == "0.130.0"
        assert record.tags == ["home-ubuntu-memstem"]

    def test_empty_body_returns_none(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "rollout.jsonl",
            [
                _session_meta_line(),
                _message_line("developer", "boilerplate"),
            ],
        )
        record = _session_to_record(path)
        assert record is None

    def test_no_cwd_omits_tag(self, tmp_path: Path) -> None:
        path = _write_session(
            tmp_path / "rollout.jsonl",
            [
                _session_meta_line(cwd=""),
                _message_line("user", "hi"),
            ],
        )
        record = _session_to_record(path)
        assert record is not None
        assert record.tags == []


class TestIsUserSkillPath:
    def test_user_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skill_md = skills / "my-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        assert _is_user_skill_path(skill_md, skills) is True

    def test_system_skill_excluded(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skill_md = skills / ".system" / "imagegen" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        assert _is_user_skill_path(skill_md, skills) is False

    def test_outside_root(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        outside = tmp_path / "elsewhere" / "SKILL.md"
        outside.parent.mkdir(parents=True)
        outside.touch()
        assert _is_user_skill_path(outside, skills) is False


class TestMarkdownToRecord:
    def test_skill_with_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "my-skill" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: my-skill\ndescription: A test skill\n---\n# My Skill\n\nBody here.\n",
            encoding="utf-8",
        )
        record = _markdown_to_record(path, "skill")
        assert record is not None
        assert record.source == "codex"
        assert record.title == "my-skill"
        assert record.metadata["type"] == "skill"
        assert "Body here." in record.body

    def test_memory_without_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "note.md"
        path.write_text("# Note Title\n\nFree-form body.\n", encoding="utf-8")
        record = _markdown_to_record(path, "memory")
        assert record is not None
        assert record.title == "Note Title"
        assert record.metadata["type"] == "memory"

    def test_unreadable_returns_none(self, tmp_path: Path) -> None:
        assert _markdown_to_record(tmp_path / "missing.md", "skill") is None


class TestReconcile:
    async def test_yields_sessions_skills_memories(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions"
        skills = tmp_path / "skills"
        memories = tmp_path / "memories"
        sessions.mkdir()
        skills.mkdir()
        memories.mkdir()

        _basic_session(sessions / "2026" / "05" / "16" / "rollout-1.jsonl")
        (skills / "my-skill").mkdir()
        (skills / "my-skill" / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n# My Skill\nBody.\n", encoding="utf-8"
        )
        (memories / "note.md").write_text("# Note\nA memory.\n", encoding="utf-8")

        adapter = CodexAdapter(sessions_root=sessions, skills_root=skills, memories_root=memories)
        records = await _drain(adapter.reconcile([]))

        types = sorted(r.metadata["type"] for r in records)
        assert types == ["memory", "session", "skill"]

    async def test_skips_system_skills(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "user-skill").mkdir(parents=True)
        (skills / "user-skill" / "SKILL.md").write_text(
            "---\nname: user-skill\n---\nBody.\n", encoding="utf-8"
        )
        (skills / ".system" / "imagegen").mkdir(parents=True)
        (skills / ".system" / "imagegen" / "SKILL.md").write_text(
            "---\nname: imagegen\n---\nVendor.\n", encoding="utf-8"
        )

        adapter = CodexAdapter(skills_root=skills)
        records = await _drain(adapter.reconcile([]))

        titles = [r.title for r in records]
        assert titles == ["user-skill"]

    async def test_missing_roots_silently_skipped(self, tmp_path: Path) -> None:
        adapter = CodexAdapter(
            sessions_root=tmp_path / "nope-sessions",
            skills_root=tmp_path / "nope-skills",
            memories_root=tmp_path / "nope-memories",
        )
        records = await _drain(adapter.reconcile([]))
        assert records == []

    async def test_all_roots_none(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        records = await _drain(adapter.reconcile([]))
        assert records == []

    async def test_ingest_disabled_per_kind(self, tmp_path: Path) -> None:
        # Simulating "skills disabled" via constructor — pass only sessions_root.
        sessions = tmp_path / "sessions"
        skills = tmp_path / "skills"
        sessions.mkdir()
        skills.mkdir()
        _basic_session(sessions / "rollout.jsonl")
        (skills / "x").mkdir()
        (skills / "x" / "SKILL.md").write_text("---\nname: x\n---\nBody.\n", encoding="utf-8")

        adapter = CodexAdapter(sessions_root=sessions, skills_root=None)
        records = await _drain(adapter.reconcile([]))
        assert [r.metadata["type"] for r in records] == ["session"]


class TestWatch:
    async def test_emits_record_on_new_session(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions" / "2026" / "05" / "16"
        sessions.mkdir(parents=True)
        adapter = CodexAdapter(sessions_root=tmp_path / "sessions")

        async def collect_one() -> MemoryRecord:
            async for rec in adapter.watch([]):
                return rec
            raise RuntimeError("watch returned without yielding")

        task = asyncio.create_task(collect_one())
        # Give the observer a tick to subscribe.
        await asyncio.sleep(0.2)
        _basic_session(sessions / "rollout-new.jsonl")

        record = await asyncio.wait_for(task, timeout=5.0)
        assert record.metadata["type"] == "session"
        assert "rollout-new.jsonl" in record.ref

    async def test_emits_record_on_new_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        adapter = CodexAdapter(skills_root=skills)

        async def collect_one() -> MemoryRecord:
            async for rec in adapter.watch([]):
                return rec
            raise RuntimeError("watch returned without yielding")

        task = asyncio.create_task(collect_one())
        await asyncio.sleep(0.2)
        skill_dir = skills / "freshly-added"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: freshly-added\n---\nNew skill body.\n", encoding="utf-8"
        )

        record = await asyncio.wait_for(task, timeout=5.0)
        assert record.metadata["type"] == "skill"
        assert record.title == "freshly-added"

    async def test_watch_ignores_system_skills(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / ".system").mkdir(parents=True)
        adapter = CodexAdapter(skills_root=skills)

        emitted: list[MemoryRecord] = []

        async def collect() -> None:
            async for rec in adapter.watch([]):
                emitted.append(rec)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)
        sys_skill_dir = skills / ".system" / "vendor-skill"
        sys_skill_dir.mkdir()
        (sys_skill_dir / "SKILL.md").write_text(
            "---\nname: vendor-skill\n---\nVendor body.\n", encoding="utf-8"
        )
        # Wait briefly to confirm nothing arrives.
        await asyncio.sleep(0.8)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert emitted == []
