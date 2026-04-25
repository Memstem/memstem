"""Tests for the FlipClaw migration helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memstem.adapters.base import MemoryRecord
from memstem.migrate import (
    MIGRATION_TAG,
    app,
    collect_claude,
    collect_openclaw,
    tag_for_migration,
)


def _write(file: Path, content: str = "# title\n\nbody\n") -> Path:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    return file


def _backdate(file: Path, days: int) -> None:
    when = time.time() - days * 86400
    os.utime(file, (when, when))


class TestTagForMigration:
    def test_adds_tag(self) -> None:
        r = MemoryRecord(source="x", ref="y", body="b", tags=["a"])
        tagged = tag_for_migration(r)
        assert MIGRATION_TAG in tagged.tags
        assert "a" in tagged.tags

    def test_idempotent(self) -> None:
        r = MemoryRecord(source="x", ref="y", body="b", tags=[MIGRATION_TAG])
        tagged = tag_for_migration(r)
        # Must not duplicate.
        assert tagged.tags.count(MIGRATION_TAG) == 1


class TestCollectOpenclaw:
    async def test_reads_all_markdown(self, tmp_path: Path) -> None:
        _write(tmp_path / "memory" / "a.md", "# A")
        _write(tmp_path / "memory" / "b.md", "# B")
        _write(tmp_path / "skills" / "x" / "SKILL.md", "# X")
        records = await collect_openclaw([tmp_path])
        assert len(records) == 3
        for r in records:
            assert MIGRATION_TAG in r.tags


class TestCollectClaude:
    async def test_filters_by_window(self, tmp_path: Path) -> None:
        recent = _write(
            tmp_path / "proj" / "recent.jsonl",
            '{"type":"user","sessionId":"r-aa","timestamp":"2026-04-25T15:00:00.000Z",'
            '"message":{"role":"user","content":"hi recent"}}\n'
            '{"type":"assistant","sessionId":"r-aa","timestamp":"2026-04-25T15:00:01.000Z",'
            '"message":{"role":"assistant","content":"hello"}}\n',
        )
        old = _write(
            tmp_path / "proj" / "old.jsonl",
            '{"type":"user","sessionId":"o-bb","timestamp":"2026-04-25T15:00:00.000Z",'
            '"message":{"role":"user","content":"hi old"}}\n'
            '{"type":"assistant","sessionId":"o-bb","timestamp":"2026-04-25T15:00:01.000Z",'
            '"message":{"role":"assistant","content":"hello"}}\n',
        )
        # Force `old` to be 100 days ago
        _backdate(old, 100)

        records = await collect_claude(days=30, root=tmp_path)
        # Only `recent` should pass the window filter.
        refs = [r.ref for r in records]
        assert str(recent) in refs
        assert str(old) not in refs
        for r in records:
            assert MIGRATION_TAG in r.tags

    async def test_empty_root(self, tmp_path: Path) -> None:
        records = await collect_claude(days=30, root=tmp_path / "missing")
        assert records == []


class TestCli:
    def test_dry_run_default(self, tmp_path: Path) -> None:
        # Initialize a vault then run the migration CLI in dry-run mode.
        from memstem.cli import app as cli_app

        vault_path = tmp_path / "vault"
        runner = CliRunner()
        runner.invoke(cli_app, ["init", str(vault_path)])

        # Seed a FlipClaw-style ari directory.
        ari = tmp_path / "ari"
        _write(ari / "memory" / "people.md", "# People\n\nbrad")

        result = runner.invoke(
            app,
            [
                "--vault",
                str(vault_path),
                "--openclaw",
                str(ari / "memory"),
                "--openclaw",
                str(ari / "skills"),
                "--claude-root",
                str(tmp_path / "no-claude"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "DRY-RUN" in result.output
        assert "1 record" in result.output
        # Vault should not have any memory files yet.
        assert list((vault_path / "memories").rglob("*.md")) == []

    def test_apply_writes_records(self, tmp_path: Path) -> None:
        from memstem.cli import app as cli_app

        vault_path = tmp_path / "vault"
        runner = CliRunner()
        runner.invoke(cli_app, ["init", str(vault_path)])

        ari = tmp_path / "ari"
        _write(ari / "memory" / "people.md", "# People\n\nbrad")
        _write(ari / "memory" / "decisions.md", "# Decisions\n\nstuff")

        result = runner.invoke(
            app,
            [
                "--apply",
                "--vault",
                str(vault_path),
                "--openclaw",
                str(ari / "memory"),
                "--openclaw",
                str(ari / "skills"),
                "--claude-root",
                str(tmp_path / "no-claude"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Applied: 2/2" in result.output
        # Two markdown files now exist under the vault.
        memory_files = list((vault_path / "memories").rglob("*.md"))
        assert len(memory_files) == 2
        # And every one carries the migration tag.
        for f in memory_files:
            assert "flipclaw-migration" in f.read_text()


@pytest.mark.parametrize("flag", ["--help"])
def test_help_renders(flag: str) -> None:
    runner = CliRunner()
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
