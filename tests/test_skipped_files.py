"""Skipped-source-file visibility: registry, adapter hooks, /health, doctor scan."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memstem.adapters.claude_code import _instructions_record
from memstem.adapters.codex import _markdown_to_record
from memstem.adapters.openclaw import _file_to_record
from memstem.config import (
    AdaptersConfig,
    ClaudeCodeAdapterConfig,
    CodexAdapterConfig,
    Config,
    OpenClawAdapterConfig,
    OpenClawWorkspace,
)
from memstem.core.index import Index
from memstem.core.skipped import SKIPPED, SkippedFiles
from memstem.core.storage import Vault
from memstem.servers.http_server import build_app

BAD = "---\ntitle: broken: [unclosed\n---\n\nbody\n"
GOOD = "---\ntitle: fixed\n---\n\nbody\n"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    SKIPPED.reset()
    yield
    SKIPPED.reset()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestRegistry:
    def test_record_clear_and_snapshot(self) -> None:
        reg = SkippedFiles()
        reg.record("/a.md", "boom", source="openclaw")
        reg.record("/b.md", "  multi\n line ", source="codex", kind="unreadable")
        assert reg.count() == 2
        snap = reg.snapshot()
        assert [f["path"] for f in snap] == ["/a.md", "/b.md"]
        assert snap[1]["reason"] == "multi line"
        assert snap[1]["kind"] == "unreadable"
        first_since = snap[0]["since"]
        reg.record("/a.md", "boom again", source="openclaw")
        assert reg.snapshot(limit=1)[0]["since"] == first_since  # first-seen is kept
        reg.clear("/a.md")
        assert reg.count() == 1


class TestAdapterHooks:
    def test_openclaw_records_then_clears_when_fixed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "memory" / "bad.md", BAD)
        assert _file_to_record(path, "openclaw") is None
        assert SKIPPED.count() == 1
        entry = SKIPPED.snapshot()[0]
        assert entry["path"] == str(path) and entry["source"] == "openclaw"
        assert entry["kind"] == "frontmatter" and entry["reason"]
        path.write_text(GOOD, encoding="utf-8")
        assert _file_to_record(path, "openclaw") is not None
        assert SKIPPED.count() == 0

    def test_claude_code_and_codex_record(self, tmp_path: Path) -> None:
        cc = _write(tmp_path / "CLAUDE.md", BAD)
        assert _instructions_record(cc, "claude-code") is None
        cx = _write(tmp_path / "skills" / "x" / "SKILL.md", BAD)
        assert _markdown_to_record(cx, "skill", "codex") is None
        assert {f["source"] for f in SKIPPED.snapshot()} == {"claude-code", "codex"}


class TestHealth:
    def test_health_exposes_block_without_degrading(self, tmp_path: Path) -> None:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        idx = Index(tmp_path / "index.db", dimensions=768)
        idx.connect()
        try:
            client = TestClient(build_app(Vault(root), idx))
            assert client.get("/health").json()["skipped_files"] == {"count": 0, "files": []}
            SKIPPED.record(
                tmp_path / "bad.md", "mapping values are not allowed here", source="openclaw"
            )
            body = client.get("/health").json()
            assert body["status"] == "ok"  # informational only
            assert body["skipped_files"]["count"] == 1
            assert body["skipped_files"]["files"][0]["path"] == str(tmp_path / "bad.md")
            assert "skipped_files" not in client.get("/health?detail=false").json()
        finally:
            idx.close()


class TestDoctorScan:
    def test_scan_finds_only_unparseable_files(self, tmp_path: Path) -> None:
        from memstem.cli import _scan_source_frontmatter

        ws = tmp_path / "ws"
        _write(ws / "memory" / "bad.md", BAD)
        _write(ws / "memory" / "good.md", GOOD)
        _write(ws / "memory" / "plain.md", "# no frontmatter at all\n")
        _write(ws / "skills" / "s" / "SKILL.md", "---\nname: s\n---\n# S\n")
        cfg = Config(
            vault_path=tmp_path / "vault",
            adapters=AdaptersConfig(
                openclaw=OpenClawAdapterConfig(
                    agent_workspaces=[OpenClawWorkspace(path=ws, tag="t")]
                )
            ),
        )
        bad = _scan_source_frontmatter(cfg)
        assert [p.name for p, _ in bad] == ["bad.md"]
        assert bad[0][1]

    def test_scan_covers_only_what_adapters_ingest(self, tmp_path: Path) -> None:
        from memstem.cli import _scan_source_frontmatter

        # Claude Code: a broken .md under a project root is NOT ingested (only
        # session JSONL + extra_files are), so it must not be reported; the
        # broken extra file must be.
        root = tmp_path / "projects"
        _write(root / "p" / "memory" / "not-ingested.md", BAD)
        extra = _write(tmp_path / "CLAUDE.md", BAD)
        # Codex: <memories_root>/*.md (flat) and user SKILL.md files; .system skipped.
        codex = tmp_path / "codex"
        _write(codex / "memories" / "bad-mem.md", BAD)
        _write(codex / "memories" / "nested" / "ignored.md", BAD)
        _write(codex / "skills" / "s" / "SKILL.md", BAD)
        _write(codex / "skills" / ".system" / "v" / "SKILL.md", BAD)
        cfg = Config(
            vault_path=tmp_path / "vault",
            adapters=AdaptersConfig(
                claude_code=ClaudeCodeAdapterConfig(project_roots=[root], extra_files=[extra]),
                codex=CodexAdapterConfig(codex_home=codex),
            ),
        )
        names = sorted(p.name for p, _ in _scan_source_frontmatter(cfg))
        assert names == ["CLAUDE.md", "SKILL.md", "bad-mem.md"]
