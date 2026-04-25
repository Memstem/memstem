"""Tests for the Memstem CLI (typer's CliRunner, in-process)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault


def _write_memory(
    vault: Vault,
    index: Index,
    *,
    title: str = "test",
    body: str = "hello world",
    type_: str = "memory",
    scope: str | None = None,
    verification: str | None = None,
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title,
    }
    if scope is not None:
        metadata["scope"] = scope
    if verification is not None:
        metadata["verification"] = verification
    fm = validate(metadata)
    path = Path("memories" if type_ != "skill" else "skills") / f"{fm.id}.md"
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    index.upsert(memory)
    return memory


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def initialized_vault(tmp_path: Path, runner: CliRunner) -> Iterator[Path]:
    vault_path = tmp_path / "vault"
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    result = runner.invoke(
        app,
        ["init", "-y", "--home", str(empty_home), str(vault_path)],
    )
    assert result.exit_code == 0, result.output
    yield vault_path


def _empty_home(tmp_path: Path) -> Path:
    home = tmp_path / "empty_home"
    home.mkdir(exist_ok=True)
    return home


class TestInit:
    def test_creates_vault_tree(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app, ["init", "-y", "--home", str(_empty_home(tmp_path)), str(vault_path)]
        )
        assert result.exit_code == 0, result.output
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            assert (vault_path / sub).is_dir()
        assert (vault_path / "_meta" / "config.yaml").is_file()

    def test_writes_default_config(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        runner.invoke(app, ["init", "-y", "--home", str(_empty_home(tmp_path)), str(vault_path)])
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "ollama"
        assert cfg["embedding"]["dimensions"] == 768
        assert cfg["search"]["rrf_k"] == 60

    def test_skips_existing_without_force(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        home = _empty_home(tmp_path)
        runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        cfg_path = vault_path / "_meta" / "config.yaml"
        cfg_path.write_text("custom: marker\n")
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        assert result.exit_code == 0
        assert "config.yaml exists" in result.output
        assert cfg_path.read_text() == "custom: marker\n"

    def test_force_overwrites(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        home = _empty_home(tmp_path)
        runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        cfg_path = vault_path / "_meta" / "config.yaml"
        cfg_path.write_text("custom: marker\n")
        result = runner.invoke(app, ["init", "-y", "--home", str(home), "--force", str(vault_path)])
        assert result.exit_code == 0
        assert "custom: marker" not in cfg_path.read_text()


class TestInitWizard:
    def _seed_agent(self, home: Path, name: str, *, with_content: bool) -> Path:
        ws = home / name
        ws.mkdir(parents=True)
        (ws / "openclaw.json").write_text("{}")
        (ws / "MEMORY.md").write_text("# core\n")
        (ws / "CLAUDE.md").write_text("# rules\n")
        if with_content:
            (ws / "memory").mkdir()
            (ws / "memory" / "people.md").write_text("# people\n")
            (ws / "skills" / "deploy").mkdir(parents=True)
            (ws / "skills" / "deploy" / "SKILL.md").write_text("# deploy\n")
        return ws

    def test_non_interactive_auto_selects_content_agents(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._seed_agent(home, "ari", with_content=True)
        self._seed_agent(home, "blake", with_content=False)
        # `blake` only has MEMORY.md + CLAUDE.md (no memory/ or skills/) — counts
        # as content because top-level files exist; we want it included too.
        # Force a true "empty" agent for the negative case:
        empty_dir = home / "ghost"
        empty_dir.mkdir()
        (empty_dir / "openclaw.json").write_text("{}")

        vault_path = tmp_path / "vault"
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        tags = {ws["tag"] for ws in cfg["adapters"]["openclaw"]["agent_workspaces"]}
        assert "ari" in tags
        assert "blake" in tags
        assert "ghost" not in tags

    def test_wizard_prompts_per_agent(self, tmp_path: Path, runner: CliRunner) -> None:
        home = tmp_path / "home"
        home.mkdir()
        self._seed_agent(home, "ari", with_content=True)
        self._seed_agent(home, "blake", with_content=True)

        vault_path = tmp_path / "vault"
        # Defaults: ari yes, blake no. Plus declines for any shared/claude prompts.
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="y\nn\n",
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        tags = {ws["tag"] for ws in cfg["adapters"]["openclaw"]["agent_workspaces"]}
        assert tags == {"ari"}

    def test_wizard_includes_shared_files(self, tmp_path: Path, runner: CliRunner) -> None:
        home = tmp_path / "home"
        home.mkdir()
        ws = self._seed_agent(home, "ari", with_content=True)
        rules = ws / "HARD-RULES.md"
        rules.write_text("# rules\n")

        vault_path = tmp_path / "vault"
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert str(rules) in cfg["adapters"]["openclaw"]["shared_files"]

    def test_wizard_picks_up_claude_code_root(self, tmp_path: Path, runner: CliRunner) -> None:
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)

        vault_path = tmp_path / "vault"
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        assert result.exit_code == 0
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        roots = cfg["adapters"]["claude_code"]["project_roots"]
        assert any("projects" in r for r in roots)


class TestSearch:
    def test_no_results(self, initialized_vault: Path, runner: CliRunner) -> None:
        result = runner.invoke(app, ["search", "nothing here", "--vault", str(initialized_vault)])
        assert result.exit_code == 0
        assert "(no results)" in result.output

    def test_finds_match(self, initialized_vault: Path, runner: CliRunner) -> None:
        # Seed via a fresh Vault+Index before invoking the CLI.
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="cloudflare doc", body="cloudflare tunnel")
        finally:
            idx.close()

        result = runner.invoke(
            app,
            ["search", "cloudflare", "--vault", str(initialized_vault)],
        )
        assert result.exit_code == 0, result.output
        assert "cloudflare doc" in result.output

    def test_filters_by_type(self, initialized_vault: Path, runner: CliRunner) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="m-alpha", body="alpha", type_="memory")
            _write_memory(
                vault,
                idx,
                title="s-alpha",
                body="alpha",
                type_="skill",
                scope="universal",
                verification="ok",
            )
        finally:
            idx.close()

        skills_only = runner.invoke(
            app,
            [
                "search",
                "alpha",
                "--vault",
                str(initialized_vault),
                "--types",
                "skill",
            ],
        )
        assert skills_only.exit_code == 0
        assert "s-alpha" in skills_only.output
        assert "m-alpha" not in skills_only.output


class TestReindex:
    def test_walks_vault_and_reports_count(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="a", body="a")
            _write_memory(vault, idx, title="b", body="b")
        finally:
            idx.close()

        result = runner.invoke(
            app,
            ["reindex", "--vault", str(initialized_vault), "--no-embed"],
        )
        assert result.exit_code == 0, result.output
        assert "reindexed 2 memories" in result.output


class TestCommands:
    def test_help_lists_all_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in ("init", "search", "reindex", "mcp", "daemon", "doctor", "connect-clients"):
            assert cmd in result.output

    def test_mcp_help(self, runner: CliRunner) -> None:
        # Don't actually run the stdio server; just verify the subcommand parses.
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "MCP server" in result.output

    def test_daemon_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["daemon", "--help"])
        assert result.exit_code == 0
        assert "daemon" in result.output.lower()


class TestDoctor:
    def test_passes_on_clean_install(
        self,
        initialized_vault: Path,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch the Ollama embedder to a stub so the doctor doesn't talk to a
        # real server.
        class _StubEmbedder:
            def __init__(self, **_: object) -> None: ...

            def embed(self, _: str) -> list[float]:
                return [0.0] * 768

            def close(self) -> None: ...

        monkeypatch.setattr("memstem.cli.OllamaEmbedder", _StubEmbedder)
        result = runner.invoke(app, ["doctor", "--vault", str(initialized_vault)])
        assert result.exit_code == 0, result.output
        assert "All checks passed" in result.output
        assert "Python 3" in result.output
        assert "Index opens cleanly" in result.output

    def test_reports_missing_vault(self, tmp_path: Path, runner: CliRunner) -> None:
        result = runner.invoke(app, ["doctor", "--vault", str(tmp_path / "no-such-vault")])
        # No vault → vault check fails, exit code 1.
        assert result.exit_code == 1
        assert "✗ Vault" in result.output

    def test_reports_unreachable_ollama(self, initialized_vault: Path, runner: CliRunner) -> None:
        # Re-write config to point Ollama at an unreachable URL.
        cfg_path = initialized_vault / "_meta" / "config.yaml"
        cfg_path.write_text(
            "vault_path: " + str(initialized_vault) + "\n"
            "embedding:\n"
            "  provider: ollama\n"
            "  model: nomic-embed-text\n"
            "  base_url: http://127.0.0.1:1\n"
            "  dimensions: 768\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["doctor", "--vault", str(initialized_vault)])
        assert result.exit_code == 1
        assert "✗ Ollama" in result.output

    def test_reports_missing_workspace(self, initialized_vault: Path, runner: CliRunner) -> None:
        cfg_path = initialized_vault / "_meta" / "config.yaml"
        cfg_path.write_text(
            "vault_path: " + str(initialized_vault) + "\n"
            "embedding:\n"
            "  provider: none\n"
            "  model: nomic-embed-text\n"
            "  base_url: http://127.0.0.1:1\n"
            "  dimensions: 768\n"
            "adapters:\n"
            "  openclaw:\n"
            "    agent_workspaces:\n"
            "      - { path: /nonexistent/agent, tag: ghost }\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["doctor", "--vault", str(initialized_vault)])
        assert result.exit_code == 1
        assert "directory missing" in result.output

    def test_doctor_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Verify the install" in result.output


class TestConnectClients:
    def _vault_with_workspace(self, tmp_path: Path, runner: CliRunner) -> tuple[Path, Path]:
        """Initialize a vault whose config points at a single OpenClaw workspace.

        Returns `(vault, workspace)`.
        """
        home = tmp_path / "home"
        ws = home / "ari"
        ws.mkdir(parents=True)
        (ws / "openclaw.json").write_text("{}")
        (ws / "MEMORY.md").write_text("# core\n")
        (ws / "CLAUDE.md").write_text("# rules\n")
        (ws / "memory").mkdir()
        (ws / "memory" / "x.md").write_text("# x\n")
        (ws / "skills" / "deploy").mkdir(parents=True)
        (ws / "skills" / "deploy" / "SKILL.md").write_text("# deploy\n")
        vault = tmp_path / "vault"
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault)])
        assert result.exit_code == 0, result.output
        return vault, ws

    def test_writes_settings_and_user_md(self, tmp_path: Path, runner: CliRunner) -> None:
        vault, _ = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(settings.read_text())
        assert data["mcpServers"]["memstem"] == {"command": "memstem", "args": ["mcp"]}
        text = user_md.read_text()
        assert "<!-- memstem:directive v1 -->" in text
        assert "<!-- /memstem:directive -->" in text

    def test_patches_workspace_claude_md_from_config(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        vault, ws = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
            ],
        )
        ws_md = (ws / "CLAUDE.md").read_text()
        assert "<!-- memstem:directive v1 -->" in ws_md
        # Pre-existing content was preserved.
        assert "# rules" in ws_md

    def test_explicit_openclaw_overrides_config(self, tmp_path: Path, runner: CliRunner) -> None:
        vault, ws = self._vault_with_workspace(tmp_path, runner)
        # Provide an explicit `--openclaw` pointing at a different file.
        other = tmp_path / "OTHER.md"
        other.write_text("# other\n")
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
                "--openclaw",
                str(other),
            ],
        )
        assert result.exit_code == 0, result.output
        # The explicit target was patched.
        assert "<!-- memstem:directive v1 -->" in other.read_text()
        # The configured workspace was NOT patched (explicit overrides config).
        assert "<!-- memstem:directive v1 -->" not in (ws / "CLAUDE.md").read_text()

    def test_dry_run_writes_nothing(self, tmp_path: Path, runner: CliRunner) -> None:
        vault, ws = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        ws_md_before = (ws / "CLAUDE.md").read_text()
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Dry run complete" in result.output
        assert not settings.exists()
        assert not user_md.exists()
        assert (ws / "CLAUDE.md").read_text() == ws_md_before

    def test_idempotent_second_run_is_noop(self, tmp_path: Path, runner: CliRunner) -> None:
        vault, ws = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
            ],
        )
        snapshot_settings = settings.read_text()
        snapshot_ws = (ws / "CLAUDE.md").read_text()
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
            ],
        )
        assert result.exit_code == 0, result.output
        # Output should report "already registered" / "already current".
        assert "already" in result.output
        # Files unchanged on the second run.
        assert settings.read_text() == snapshot_settings
        assert (ws / "CLAUDE.md").read_text() == snapshot_ws

    def test_no_claude_code_skips_settings(self, tmp_path: Path, runner: CliRunner) -> None:
        vault, ws = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        user_md = tmp_path / "CLAUDE.md"
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
                "--no-claude-code",
            ],
        )
        assert result.exit_code == 0, result.output
        assert not settings.exists()
        assert not user_md.exists()
        # Workspace CLAUDE.md still patched.
        assert "<!-- memstem:directive v1 -->" in (ws / "CLAUDE.md").read_text()

    def test_remove_flipclaw_strips_session_end_hook(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        vault, _ = self._vault_with_workspace(tmp_path, runner)
        settings = tmp_path / "settings.json"
        # Pre-seed settings with a FlipClaw-style hook.
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python3 /home/x/claude-code-bridge.py",
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )
        user_md = tmp_path / "CLAUDE.md"
        result = runner.invoke(
            app,
            [
                "connect-clients",
                "--vault",
                str(vault),
                "--settings",
                str(settings),
                "--claude-md",
                str(user_md),
                "--remove-flipclaw",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(settings.read_text())
        # Memstem registered AND FlipClaw hook removed.
        assert "memstem" in data["mcpServers"]
        assert "SessionEnd" not in data.get("hooks", {})

    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["connect-clients", "--help"])
        assert result.exit_code == 0
        # Rich may wrap or style flag names depending on terminal width, so we
        # only assert on the stable docstring text.
        assert "wire memstem" in result.output.lower()
