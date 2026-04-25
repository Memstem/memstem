"""Tests for the canonical markdown vault layer."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import pytest
from memstem.core.frontmatter import Frontmatter, MemoryType, validate
from memstem.core.storage import (
    InvalidFrontmatterError,
    Memory,
    MemoryNotFoundError,
    PathEscapesVaultError,
    Vault,
)


def _make_memory(
    path: str,
    *,
    type_: str = "memory",
    title: str | None = "test",
    body: str = "hello world",
) -> Memory:
    fm_obj = validate(
        {
            "id": str(uuid4()),
            "type": type_,
            "created": "2026-04-25T15:00:00+00:00",
            "updated": "2026-04-25T15:00:00+00:00",
            "source": "human",
            "title": title,
        }
    )
    return Memory(frontmatter=fm_obj, body=body, path=Path(path))


def _write_raw(vault_root: Path, rel_path: str, contents: str) -> None:
    full = vault_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(contents, encoding="utf-8")


class TestRoundTrip:
    def test_write_then_read(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        memory = _make_memory("memories/test.md", body="my content")
        vault.write(memory)
        loaded = vault.read("memories/test.md")
        assert loaded.id == memory.id
        assert loaded.body == "my content"
        assert loaded.frontmatter.title == "test"

    def test_write_creates_parent_dirs(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        memory = _make_memory("memories/people/deep/nested.md")
        vault.write(memory)
        assert (tmp_vault / "memories/people/deep/nested.md").is_file()

    def test_read_resolves_absolute_path(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        memory = _make_memory("memories/abs.md")
        vault.write(memory)
        loaded = vault.read(tmp_vault / "memories/abs.md")
        assert loaded.id == memory.id


class TestErrors:
    def test_read_missing_raises(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        with pytest.raises(MemoryNotFoundError):
            vault.read("memories/missing.md")

    def test_delete_missing_raises(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        with pytest.raises(MemoryNotFoundError):
            vault.delete("memories/missing.md")

    def test_invalid_frontmatter_on_read(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        _write_raw(tmp_vault, "memories/bad.md", "---\ntype: memory\n---\nbody\n")
        with pytest.raises(InvalidFrontmatterError):
            vault.read("memories/bad.md")

    def test_path_escape_rejected(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        with pytest.raises(PathEscapesVaultError):
            vault.read("../escape.md")
        with pytest.raises(PathEscapesVaultError):
            vault.read("/etc/passwd")


class TestDelete:
    def test_delete_removes_file(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        memory = _make_memory("memories/gone.md")
        vault.write(memory)
        vault.delete("memories/gone.md")
        assert not (tmp_vault / "memories/gone.md").exists()


class TestWalk:
    def test_walk_finds_all_memories(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        vault.write(_make_memory("memories/a.md", title="a"))
        vault.write(_make_memory("memories/b.md", title="b"))
        scoped_skill = Memory(
            frontmatter=Frontmatter.model_validate(
                {
                    "id": str(uuid4()),
                    "type": "skill",
                    "created": "2026-04-25T15:00:00+00:00",
                    "updated": "2026-04-25T15:00:00+00:00",
                    "source": "human",
                    "title": "skill-c",
                    "scope": "universal",
                    "verification": "verify",
                }
            ),
            body="",
            path=Path("skills/c.md"),
        )
        vault.write(scoped_skill)
        titles = sorted(m.frontmatter.title or "" for m in vault.walk())
        assert titles == ["a", "b", "skill-c"]

    def test_walk_filters_by_type(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        vault.write(_make_memory("memories/a.md"))
        vault.write(
            Memory(
                frontmatter=Frontmatter.model_validate(
                    {
                        "id": str(uuid4()),
                        "type": "skill",
                        "created": "2026-04-25T15:00:00+00:00",
                        "updated": "2026-04-25T15:00:00+00:00",
                        "source": "human",
                        "title": "s",
                        "scope": "universal",
                        "verification": "v",
                    }
                ),
                body="",
                path=Path("skills/s.md"),
            )
        )
        memories_only = list(vault.walk(types=["memory"]))
        skills_only = list(vault.walk(types=["skill"]))
        assert len(memories_only) == 1
        assert memories_only[0].type is MemoryType.MEMORY
        assert len(skills_only) == 1
        assert skills_only[0].type is MemoryType.SKILL

    def test_walk_skips_meta_dir(self, tmp_vault: Path) -> None:
        vault = Vault(tmp_vault)
        vault.write(_make_memory("memories/real.md"))
        _write_raw(tmp_vault, "_meta/notes.md", "should be ignored")
        results = list(vault.walk())
        assert len(results) == 1

    def test_walk_skips_invalid_frontmatter_with_warning(
        self, tmp_vault: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        vault = Vault(tmp_vault)
        vault.write(_make_memory("memories/good.md"))
        _write_raw(tmp_vault, "memories/bad.md", "---\ntype: memory\n---\nbody\n")
        with caplog.at_level(logging.WARNING):
            results = list(vault.walk())
        assert len(results) == 1
        assert any("bad.md" in record.message for record in caplog.records)
