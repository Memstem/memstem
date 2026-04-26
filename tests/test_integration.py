"""Tests for `memstem.integration` (settings.json + CLAUDE.md edits)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memstem.integration import (
    DEFAULT_MCP_SERVER_ENTRY,
    DEFAULT_OPENCLAW_MCP_SERVER_ENTRY,
    DIRECTIVE_BEGIN,
    DIRECTIVE_BLOCK,
    DIRECTIVE_END,
    apply_directive,
    claude_md_targets_for_openclaw,
    mcp_env_from_embedding,
    openclaw_config_for_workspace,
    register_mcp_server,
    register_openclaw_mcp_server,
    remove_flipclaw_hook,
    remove_legacy_mcp_server,
)


class TestRegisterMcpServer:
    def test_creates_settings_when_missing(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        change = register_mcp_server(settings)
        assert change.action == "created"
        assert settings.is_file()
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"] == DEFAULT_MCP_SERVER_ENTRY

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        settings = tmp_path / "claude-home" / "settings.json"
        change = register_mcp_server(settings)
        assert change.action == "created"
        assert settings.parent.is_dir()

    def test_merges_with_existing_unrelated_keys(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"model": "opus[1m]", "permissions": {"allow": ["Bash(ls:*)"]}})
        )
        change = register_mcp_server(settings)
        assert change.action == "updated"
        data = json.loads(settings.read_text())
        assert data["model"] == "opus[1m]"
        assert data["permissions"]["allow"] == ["Bash(ls:*)"]
        assert data["mcpServers"]["memstem"] == DEFAULT_MCP_SERVER_ENTRY

    def test_preserves_other_mcp_servers(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp"]},
                    }
                }
            )
        )
        register_mcp_server(settings)
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["playwright"] == {
            "command": "npx",
            "args": ["-y", "@playwright/mcp"],
        }
        assert data["mcpServers"]["memstem"] == DEFAULT_MCP_SERVER_ENTRY

    def test_idempotent_when_already_registered(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        register_mcp_server(settings)
        # Capture the current content; a no-op should leave the file
        # untouched (and importantly, not write a fresh .bak).
        before = settings.read_text()
        bak = settings.with_suffix(".json.bak")
        bak_existed_before = bak.exists()
        change = register_mcp_server(settings)
        assert change.action == "noop"
        assert settings.read_text() == before
        # The previous .bak (if any) is preserved; we should not have
        # silently overwritten it for a no-op.
        assert bak.exists() == bak_existed_before

    def test_writes_backup_before_editing(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = json.dumps({"existing": True})
        settings.write_text(original)
        change = register_mcp_server(settings)
        assert change.backup_path is not None
        assert change.backup_path.exists()
        assert change.backup_path.read_text() == original

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        change = register_mcp_server(settings, dry_run=True)
        assert change.action == "created"
        assert change.diff
        assert not settings.exists()

    def test_dry_run_on_existing_does_not_write_backup(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = json.dumps({"existing": True})
        settings.write_text(original)
        change = register_mcp_server(settings, dry_run=True)
        assert change.action == "updated"
        assert settings.read_text() == original
        assert not settings.with_suffix(".json.bak").exists()

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{ this is not json")
        with pytest.raises(ValueError):
            register_mcp_server(settings)

    def test_top_level_array_raises(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError):
            register_mcp_server(settings)

    def test_custom_entry_overrides_default(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        custom = {"command": "/usr/local/bin/memstem", "args": ["mcp", "--vault", "/v"]}
        register_mcp_server(settings, entry=custom)
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"] == custom


class TestRemoveLegacyMcpServer:
    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        change = remove_legacy_mcp_server(settings)
        assert change.action == "noop"
        assert not settings.exists()

    def test_noop_when_entry_absent(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": []}}})
        )
        before = settings.read_text()
        change = remove_legacy_mcp_server(settings)
        assert change.action == "noop"
        assert settings.read_text() == before

    def test_strips_entry_and_preserves_others(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "model": "opus[1m]",
                    "mcpServers": {
                        "memstem": {"command": "memstem", "args": ["mcp"]},
                        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp"]},
                    },
                }
            )
        )
        change = remove_legacy_mcp_server(settings)
        assert change.action == "updated"
        assert change.backup_path is not None and change.backup_path.exists()
        data = json.loads(settings.read_text())
        assert data["model"] == "opus[1m]"
        assert "memstem" not in data["mcpServers"]
        assert data["mcpServers"]["playwright"] == {
            "command": "npx",
            "args": ["-y", "@playwright/mcp"],
        }

    def test_removes_empty_mcpServers_key(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "model": "opus[1m]",
                    "mcpServers": {"memstem": {"command": "memstem", "args": ["mcp"]}},
                }
            )
        )
        remove_legacy_mcp_server(settings)
        data = json.loads(settings.read_text())
        assert "mcpServers" not in data
        assert data["model"] == "opus[1m]"

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = json.dumps({"mcpServers": {"memstem": {"command": "memstem", "args": ["mcp"]}}})
        settings.write_text(original)
        change = remove_legacy_mcp_server(settings, dry_run=True)
        assert change.action == "updated"
        assert settings.read_text() == original
        assert not settings.with_suffix(".json.bak").exists()

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{ this is not json")
        with pytest.raises(ValueError):
            remove_legacy_mcp_server(settings)


class TestApplyDirective:
    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        change = apply_directive(path)
        assert change.action == "noop"
        assert not path.exists()

    def test_create_if_missing_writes_new_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "CLAUDE.md"
        change = apply_directive(path, create_if_missing=True)
        assert change.action == "created"
        assert path.is_file()
        assert DIRECTIVE_BEGIN in path.read_text()
        assert DIRECTIVE_END in path.read_text()

    def test_appends_when_marker_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        path.write_text("# Pre-existing instructions\n\nLine two.\n")
        change = apply_directive(path)
        assert change.action == "updated"
        text = path.read_text()
        assert "# Pre-existing instructions" in text
        assert DIRECTIVE_BEGIN in text
        assert DIRECTIVE_END in text
        # Existing content must come first.
        assert text.index("# Pre-existing instructions") < text.index(DIRECTIVE_BEGIN)

    def test_appends_with_blank_line_separator(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        path.write_text("# header\n")
        apply_directive(path)
        text = path.read_text()
        # There should be at least one blank line between the existing
        # content and the directive block so renderers don't collapse them.
        assert "\n\n" + DIRECTIVE_BEGIN in text

    def test_replaces_existing_block_in_place(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        old = f"# top\n\n{DIRECTIVE_BEGIN}\nold body content\n{DIRECTIVE_END}\n\n# bottom\n"
        path.write_text(old)
        change = apply_directive(path)
        assert change.action == "updated"
        text = path.read_text()
        assert "old body content" not in text
        assert "# top" in text
        assert "# bottom" in text
        # The new block must sit between the two headers.
        assert text.index("# top") < text.index(DIRECTIVE_BEGIN)
        assert text.index(DIRECTIVE_END) < text.index("# bottom")

    def test_idempotent_when_block_current(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        path.write_text(f"# top\n\n{DIRECTIVE_BLOCK}")
        change = apply_directive(path)
        assert change.action == "noop"
        # No backup written for a no-op.
        assert not path.with_suffix(".md.bak").exists()

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        original = "# header\n"
        path.write_text(original)
        change = apply_directive(path, dry_run=True)
        assert change.action == "updated"
        assert change.diff
        assert path.read_text() == original

    def test_writes_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        original = "# header\n"
        path.write_text(original)
        change = apply_directive(path)
        assert change.backup_path is not None
        assert change.backup_path.read_text() == original

    def test_handles_no_trailing_newline(self, tmp_path: Path) -> None:
        path = tmp_path / "CLAUDE.md"
        path.write_text("# no newline at end")
        change = apply_directive(path)
        assert change.action == "updated"
        text = path.read_text()
        # Block should still be present and well-formed.
        assert text.endswith(DIRECTIVE_END + "\n")


class TestRemoveFlipclawHook:
    def _settings_with_hook(self, marker: str = "claude-code-bridge.py") -> dict[str, Any]:
        return {
            "model": "opus[1m]",
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "python3 /home/x/turn-capture.py"}]}
                ],
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": f"python3 /home/x/{marker}"}]}
                ],
            },
        }

    def test_removes_matching_hook(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps(self._settings_with_hook()))
        change = remove_flipclaw_hook(settings)
        assert change.action == "updated"
        data = json.loads(settings.read_text())
        # The unrelated `Stop` hook stays.
        assert "Stop" in data["hooks"]
        # SessionEnd is empty → key was pruned.
        assert "SessionEnd" not in data["hooks"]

    def test_preserves_unrelated_session_end_hooks(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        cfg = {
            "hooks": {
                "SessionEnd": [
                    {
                        "hooks": [
                            {"type": "command", "command": "python3 /home/x/claude-code-bridge.py"},
                            {"type": "command", "command": "python3 /home/x/keep-me.py"},
                        ]
                    }
                ]
            }
        }
        settings.write_text(json.dumps(cfg))
        change = remove_flipclaw_hook(settings)
        assert change.action == "updated"
        data = json.loads(settings.read_text())
        cmds = [h["command"] for h in data["hooks"]["SessionEnd"][0]["hooks"]]
        assert any("keep-me.py" in c for c in cmds)
        assert not any("claude-code-bridge.py" in c for c in cmds)

    def test_noop_when_hook_absent(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": []}}))
        change = remove_flipclaw_hook(settings)
        assert change.action == "noop"
        # No backup created for a no-op.
        assert not settings.with_suffix(".json.bak").exists()

    def test_noop_when_no_hooks_block(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"model": "opus"}))
        change = remove_flipclaw_hook(settings)
        assert change.action == "noop"

    def test_noop_when_settings_missing(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        change = remove_flipclaw_hook(settings)
        assert change.action == "noop"
        assert not settings.exists()

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = json.dumps(self._settings_with_hook())
        settings.write_text(original)
        change = remove_flipclaw_hook(settings, dry_run=True)
        assert change.action == "updated"
        assert change.diff
        assert settings.read_text() == original

    def test_writes_backup(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = json.dumps(self._settings_with_hook())
        settings.write_text(original)
        change = remove_flipclaw_hook(settings)
        assert change.backup_path is not None
        assert change.backup_path.read_text() == original

    def test_custom_marker(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps(self._settings_with_hook(marker="legacy-bridge.py")))
        change = remove_flipclaw_hook(settings, marker="legacy-bridge.py")
        assert change.action == "updated"

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not json")
        with pytest.raises(ValueError):
            remove_flipclaw_hook(settings)


class TestClaudeMdTargetsForOpenclaw:
    def test_resolves_workspace_dir(self, tmp_path: Path) -> None:
        ws = tmp_path / "ari"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# rules\n")
        targets = claude_md_targets_for_openclaw(ws)
        assert targets == [ws / "CLAUDE.md"]

    def test_resolves_direct_file(self, tmp_path: Path) -> None:
        f = tmp_path / "AGENT.md"
        f.write_text("# x\n")
        assert claude_md_targets_for_openclaw(f) == [f]

    def test_returns_empty_for_workspace_without_claude_md(self, tmp_path: Path) -> None:
        ws = tmp_path / "ari"
        ws.mkdir()
        assert claude_md_targets_for_openclaw(ws) == []

    def test_returns_empty_for_nonexistent_path(self, tmp_path: Path) -> None:
        assert claude_md_targets_for_openclaw(tmp_path / "nope") == []


class TestOpenclawConfigForWorkspace:
    def test_resolves_workspace_dir(self, tmp_path: Path) -> None:
        ws = tmp_path / "ari"
        ws.mkdir()
        cfg = ws / "openclaw.json"
        cfg.write_text(json.dumps({"meta": {"name": "ari"}}))
        assert openclaw_config_for_workspace(ws) == cfg

    def test_resolves_direct_openclaw_json(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps({"meta": {"name": "ari"}}))
        assert openclaw_config_for_workspace(cfg) == cfg

    def test_finds_sibling_when_passed_claude_md(self, tmp_path: Path) -> None:
        ws = tmp_path / "ari"
        ws.mkdir()
        cfg = ws / "openclaw.json"
        cfg.write_text(json.dumps({}))
        md = ws / "CLAUDE.md"
        md.write_text("# x")
        assert openclaw_config_for_workspace(md) == cfg

    def test_returns_none_for_workspace_without_openclaw_json(self, tmp_path: Path) -> None:
        ws = tmp_path / "ari"
        ws.mkdir()
        assert openclaw_config_for_workspace(ws) is None

    def test_returns_none_for_claude_md_without_sibling(self, tmp_path: Path) -> None:
        md = tmp_path / "CLAUDE.md"
        md.write_text("# x")
        assert openclaw_config_for_workspace(md) is None

    def test_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        assert openclaw_config_for_workspace(tmp_path / "nope") is None


class TestRegisterOpenclawMcpServer:
    def _seed_config(self, path: Path, *, mcp: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write an openclaw.json with realistic top-level structure plus optional mcp block."""
        data: dict[str, Any] = {
            "meta": {"name": "ari", "version": 1},
            "agents": {"main": {"model": "opus[1m]"}},
            "tools": {"web": {"enabled": True}},
        }
        if mcp is not None:
            data["mcp"] = mcp
        path.write_text(json.dumps(data, indent=2))
        return data

    def test_registers_when_mcp_block_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        change = register_openclaw_mcp_server(cfg)
        assert change.action == "updated"
        data = json.loads(cfg.read_text())
        assert data["mcp"]["servers"]["memstem"] == DEFAULT_OPENCLAW_MCP_SERVER_ENTRY
        # Other top-level keys are preserved.
        assert data["meta"]["name"] == "ari"
        assert data["agents"]["main"]["model"] == "opus[1m]"
        assert data["tools"]["web"]["enabled"] is True

    def test_preserves_other_servers(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(
            cfg,
            mcp={"servers": {"context7": {"command": "uvx", "args": ["context7-mcp"]}}},
        )
        register_openclaw_mcp_server(cfg)
        data = json.loads(cfg.read_text())
        assert data["mcp"]["servers"]["context7"] == {
            "command": "uvx",
            "args": ["context7-mcp"],
        }
        assert data["mcp"]["servers"]["memstem"] == DEFAULT_OPENCLAW_MCP_SERVER_ENTRY

    def test_idempotent_when_already_registered(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        register_openclaw_mcp_server(cfg)
        before = cfg.read_text()
        bak = cfg.with_suffix(".json.bak")
        bak_existed_before = bak.exists()
        change = register_openclaw_mcp_server(cfg)
        assert change.action == "noop"
        assert cfg.read_text() == before
        # No-op should not re-write the .bak.
        assert bak.exists() == bak_existed_before

    def test_writes_backup_before_editing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        original = cfg.read_text()
        change = register_openclaw_mcp_server(cfg)
        assert change.backup_path is not None and change.backup_path.exists()
        assert change.backup_path.read_text() == original

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        original = cfg.read_text()
        change = register_openclaw_mcp_server(cfg, dry_run=True)
        assert change.action == "updated"
        assert change.diff
        assert cfg.read_text() == original
        assert not cfg.with_suffix(".json.bak").exists()

    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        change = register_openclaw_mcp_server(cfg)
        assert change.action == "noop"
        assert not cfg.exists()

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        cfg.write_text("{ not json")
        with pytest.raises(ValueError):
            register_openclaw_mcp_server(cfg)

    def test_top_level_array_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError):
            register_openclaw_mcp_server(cfg)

    def test_non_dict_mcp_block_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        # Manually write a non-dict mcp block to provoke the guard.
        data = json.loads(cfg.read_text())
        data["mcp"] = ["not a dict"]
        cfg.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            register_openclaw_mcp_server(cfg)

    def test_custom_entry_overrides_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        custom = {"command": "/opt/memstem/bin/memstem", "args": ["mcp", "--vault", "/v"]}
        register_openclaw_mcp_server(cfg, entry=custom)
        data = json.loads(cfg.read_text())
        assert data["mcp"]["servers"]["memstem"] == custom


class TestMcpEnvFromEmbedding:
    """`mcp_env_from_embedding` resolves the embedder's API key into an
    env dict that gets baked into the MCP registration. Without this,
    a Claude Code or OpenClaw spawn of `memstem mcp` runs without the
    key, the embedder fails to instantiate, and Search silently falls
    back to BM25-only."""

    def test_returns_key_value_when_set(self) -> None:
        env = {"GEMINI_API_KEY": "test-key-abc"}
        out = mcp_env_from_embedding("GEMINI_API_KEY", process_env=env)
        assert out == {"GEMINI_API_KEY": "test-key-abc"}

    def test_empty_when_var_missing(self) -> None:
        out = mcp_env_from_embedding("GEMINI_API_KEY", process_env={})
        assert out == {}

    def test_empty_when_var_blank(self) -> None:
        # Whitespace-only is treated as missing — easier than a 39-char
        # key with whitespace getting through (which would fail at the
        # embedder side anyway).
        out = mcp_env_from_embedding("GEMINI_API_KEY", process_env={"GEMINI_API_KEY": "   "})
        assert out == {}

    def test_none_api_key_env_returns_empty(self) -> None:
        # Local providers (Ollama) don't have an api_key_env.
        out = mcp_env_from_embedding(None, process_env={"GEMINI_API_KEY": "x"})
        assert out == {}

    def test_uses_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMSTEM_TEST_KEY", "from-shell")
        out = mcp_env_from_embedding("MEMSTEM_TEST_KEY")
        assert out == {"MEMSTEM_TEST_KEY": "from-shell"}


class TestRegisterMcpServerWithEnv:
    """`env` parameter merges into the entry's env block (Claude Code side)."""

    def test_env_kwarg_populates_entry_env(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        register_mcp_server(settings, env={"GEMINI_API_KEY": "abc"})
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"]["env"] == {"GEMINI_API_KEY": "abc"}

    def test_env_kwarg_merges_with_existing_default(self, tmp_path: Path) -> None:
        # The default entry has env={}; merging a key adds to it.
        settings = tmp_path / "settings.json"
        register_mcp_server(settings, env={"FOO": "bar"})
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"] == {
            **DEFAULT_MCP_SERVER_ENTRY,
            "env": {"FOO": "bar"},
        }

    def test_env_none_preserves_default_empty_env(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        register_mcp_server(settings)
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"]["env"] == {}

    def test_env_kwarg_does_not_mutate_default_constant(self, tmp_path: Path) -> None:
        # Catch a class of bugs where a function mutates a module-level
        # default by aliasing instead of copying.
        before = dict(DEFAULT_MCP_SERVER_ENTRY)
        register_mcp_server(tmp_path / "s.json", env={"X": "y"})
        assert DEFAULT_MCP_SERVER_ENTRY == before

    def test_env_with_explicit_entry(self, tmp_path: Path) -> None:
        # When `entry` is also passed, env merges into entry's env block.
        settings = tmp_path / "settings.json"
        custom_entry = {"command": "/opt/memstem", "args": ["mcp"], "env": {"PRESET": "1"}}
        register_mcp_server(settings, entry=custom_entry, env={"GEMINI_API_KEY": "k"})
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"]["env"] == {"PRESET": "1", "GEMINI_API_KEY": "k"}


class TestRegisterOpenclawMcpServerWithEnv:
    """`env` parameter on the OpenClaw side adds an `env` block to the
    entry, which the default OpenClaw shape doesn't include otherwise."""

    @staticmethod
    def _seed_config(cfg: Path) -> None:
        cfg.write_text(json.dumps({"mcp": {"servers": {}}}, indent=2))

    def test_env_kwarg_adds_env_block(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        register_openclaw_mcp_server(cfg, env={"GEMINI_API_KEY": "k"})
        data = json.loads(cfg.read_text())
        assert data["mcp"]["servers"]["memstem"]["env"] == {"GEMINI_API_KEY": "k"}

    def test_env_none_preserves_no_env_block(self, tmp_path: Path) -> None:
        # Default OpenClaw entry has no `env`; without env=, we shouldn't
        # introduce one. Important for local-Ollama installs.
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        register_openclaw_mcp_server(cfg)
        data = json.loads(cfg.read_text())
        assert "env" not in data["mcp"]["servers"]["memstem"]

    def test_empty_env_dict_preserves_no_env_block(self, tmp_path: Path) -> None:
        # An empty dict (e.g., from a local-Ollama config or unset key)
        # should be treated like None — don't add an empty env block.
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        register_openclaw_mcp_server(cfg, env={})
        data = json.loads(cfg.read_text())
        assert "env" not in data["mcp"]["servers"]["memstem"]

    def test_env_kwarg_does_not_mutate_default_constant(self, tmp_path: Path) -> None:
        cfg = tmp_path / "openclaw.json"
        self._seed_config(cfg)
        before = dict(DEFAULT_OPENCLAW_MCP_SERVER_ENTRY)
        register_openclaw_mcp_server(cfg, env={"X": "y"})
        assert DEFAULT_OPENCLAW_MCP_SERVER_ENTRY == before
