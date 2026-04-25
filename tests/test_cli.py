"""Tests for the Memstem CLI (typer's CliRunner, in-process)."""

from __future__ import annotations

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
    result = runner.invoke(app, ["init", str(vault_path)])
    assert result.exit_code == 0
    yield vault_path


class TestInit:
    def test_creates_vault_tree(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(app, ["init", str(vault_path)])
        assert result.exit_code == 0, result.output
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            assert (vault_path / sub).is_dir()
        assert (vault_path / "_meta" / "config.yaml").is_file()

    def test_writes_default_config(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        runner.invoke(app, ["init", str(vault_path)])
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "ollama"
        assert cfg["embedding"]["dimensions"] == 768
        assert cfg["search"]["rrf_k"] == 60

    def test_skips_existing_without_force(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        runner.invoke(app, ["init", str(vault_path)])
        # Mark the config so we can verify it wasn't overwritten.
        cfg_path = vault_path / "_meta" / "config.yaml"
        cfg_path.write_text("custom: marker\n")

        result = runner.invoke(app, ["init", str(vault_path)])
        assert result.exit_code == 0
        assert "config.yaml exists" in result.output
        assert cfg_path.read_text() == "custom: marker\n"

    def test_force_overwrites(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        runner.invoke(app, ["init", str(vault_path)])
        cfg_path = vault_path / "_meta" / "config.yaml"
        cfg_path.write_text("custom: marker\n")

        result = runner.invoke(app, ["init", str(vault_path), "--force"])
        assert result.exit_code == 0
        assert "custom: marker" not in cfg_path.read_text()


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
        for cmd in ("init", "search", "reindex", "mcp", "daemon", "doctor"):
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
