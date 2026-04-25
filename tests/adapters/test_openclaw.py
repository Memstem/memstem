"""Tests for the OpenClaw filesystem adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from memstem.adapters.base import MemoryRecord
from memstem.adapters.openclaw import (
    OpenClawAdapter,
    _classify_type,
    _extract_h1,
    _file_to_record,
)


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
