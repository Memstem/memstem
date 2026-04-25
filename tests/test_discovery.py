"""Tests for the OpenClaw / Claude Code auto-discovery helpers."""

from __future__ import annotations

from pathlib import Path

from memstem.discovery import (
    OpenClawCandidate,
    build_default_adapters_config,
    discover_claude_code_extras,
    discover_claude_code_root,
    discover_openclaw_candidates,
    discover_shared_files,
)


def _make_workspace(home: Path, name: str, *, content: bool = False) -> Path:
    ws = home / name
    ws.mkdir(parents=True)
    (ws / "openclaw.json").write_text("{}")
    (ws / "MEMORY.md").write_text("# core\n")
    (ws / "CLAUDE.md").write_text("# rules\n")
    if content:
        (ws / "memory").mkdir()
        (ws / "memory" / "people.md").write_text("# people\n")
        (ws / "skills" / "deploy").mkdir(parents=True)
        (ws / "skills" / "deploy" / "SKILL.md").write_text("# skill\n")
    return ws


class TestDiscoverOpenClawCandidates:
    def test_finds_workspaces_with_openclaw_json(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, "ari", content=True)
        _make_workspace(tmp_path, "blake")
        # A non-agent dir without openclaw.json
        (tmp_path / "scratch").mkdir()
        (tmp_path / "scratch" / "MEMORY.md").write_text("# decoy\n")

        candidates = discover_openclaw_candidates(tmp_path)
        tags = {c.tag for c in candidates}
        assert tags == {"ari", "blake"}

    def test_returns_empty_for_empty_home(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert discover_openclaw_candidates(empty) == []

    def test_returns_empty_for_nonexistent_home(self, tmp_path: Path) -> None:
        assert discover_openclaw_candidates(tmp_path / "nope") == []

    def test_candidate_counts_files(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, "ari", content=True)
        candidates = discover_openclaw_candidates(tmp_path)
        cand = next(c for c in candidates if c.tag == "ari")
        assert cand.has_memory_md is True
        assert cand.has_claude_md is True
        assert cand.memory_files == 1
        assert cand.skill_files == 1
        assert cand.has_content is True

    def test_candidate_with_only_top_files_still_has_content(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, "blake", content=False)
        candidates = discover_openclaw_candidates(tmp_path)
        cand = next(c for c in candidates if c.tag == "blake")
        assert cand.memory_files == 0
        assert cand.skill_files == 0
        # MEMORY.md/CLAUDE.md count as content for inclusion purposes.
        assert cand.has_content is True

    def test_describe_format(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, "ari", content=True)
        cand = discover_openclaw_candidates(tmp_path)[0]
        described = cand.describe()
        assert "MEMORY.md ✓" in described
        assert "CLAUDE.md ✓" in described
        assert "1 memory" in described
        assert "1 skills" in described


class TestDiscoverSharedFiles:
    def test_finds_hard_rules(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, "ari")
        rules = ws / "HARD-RULES.md"
        rules.write_text("# rules\n")
        assert discover_shared_files(tmp_path) == [rules]

    def test_skips_non_agent_dirs(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "HARD-RULES.md").write_text("# decoy\n")
        assert discover_shared_files(tmp_path) == []


class TestDiscoverClaudeCode:
    def test_finds_projects_root_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "projects").mkdir(parents=True)
        result = discover_claude_code_root(tmp_path)
        assert result == tmp_path / ".claude" / "projects"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert discover_claude_code_root(tmp_path) is None

    def test_finds_user_claude_md(self, tmp_path: Path) -> None:
        cmd = tmp_path / ".claude" / "CLAUDE.md"
        cmd.parent.mkdir(parents=True)
        cmd.write_text("# instructions\n")
        assert discover_claude_code_extras(tmp_path) == [cmd]


class TestBuildDefaultAdaptersConfig:
    def test_includes_only_content_workspaces(self, tmp_path: Path) -> None:
        _make_workspace(tmp_path, "ari", content=True)
        # An agent dir with only openclaw.json — no MEMORY/CLAUDE/memory/skills.
        empty_dir = tmp_path / "ghost"
        empty_dir.mkdir()
        (empty_dir / "openclaw.json").write_text("{}")

        cfg = build_default_adapters_config(tmp_path)
        tags = {ws.tag for ws in cfg.openclaw.agent_workspaces}
        assert tags == {"ari"}

    def test_picks_up_shared_and_claude_paths(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, "ari", content=True)
        rules = ws / "HARD-RULES.md"
        rules.write_text("# rules\n")
        (tmp_path / ".claude" / "projects").mkdir(parents=True)
        (tmp_path / ".claude" / "CLAUDE.md").write_text("# instructions\n")

        cfg = build_default_adapters_config(tmp_path)
        assert rules in cfg.openclaw.shared_files
        assert (tmp_path / ".claude" / "projects") in cfg.claude_code.project_roots
        assert (tmp_path / ".claude" / "CLAUDE.md") in cfg.claude_code.extra_files


class TestOpenClawCandidate:
    def test_has_content_with_no_files(self, tmp_path: Path) -> None:
        cand = OpenClawCandidate(
            workspace=tmp_path,
            tag="x",
            has_memory_md=False,
            has_claude_md=False,
            memory_files=0,
            skill_files=0,
        )
        assert cand.has_content is False
