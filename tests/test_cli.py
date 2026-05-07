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
from memstem.integration import DEFAULT_MCP_SERVER_ENTRY


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

    def test_provider_openai_uses_known_good_defaults(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app,
            [
                "init",
                "-y",
                "--home",
                str(_empty_home(tmp_path)),
                "--provider",
                "openai",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "openai"
        assert cfg["embedding"]["model"] == "text-embedding-3-large"
        assert cfg["embedding"]["dimensions"] == 3072
        assert cfg["embedding"]["api_key_env"] == "OPENAI_API_KEY"
        # The init output guides the user toward `memstem auth set`
        assert "memstem auth set openai" in result.output

    def test_provider_gemini(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app,
            [
                "init",
                "-y",
                "--home",
                str(_empty_home(tmp_path)),
                "--provider",
                "gemini",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "gemini"
        assert cfg["embedding"]["api_key_env"] == "GEMINI_API_KEY"

    def test_provider_voyage(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app,
            [
                "init",
                "-y",
                "--home",
                str(_empty_home(tmp_path)),
                "--provider",
                "voyage",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "voyage"
        assert cfg["embedding"]["model"] == "voyage-3"
        assert cfg["embedding"]["dimensions"] == 1024

    def test_provider_ollama_explicit_matches_default(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app,
            [
                "init",
                "-y",
                "--home",
                str(_empty_home(tmp_path)),
                "--provider",
                "ollama",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["embedding"]["provider"] == "ollama"
        # Ollama doesn't need an API key
        assert cfg["embedding"]["api_key_env"] is None

    def test_provider_unknown_exits_2(self, tmp_path: Path, runner: CliRunner) -> None:
        vault_path = tmp_path / "fresh"
        result = runner.invoke(
            app,
            [
                "init",
                "-y",
                "--home",
                str(_empty_home(tmp_path)),
                "--provider",
                "bogus",
                str(vault_path),
            ],
        )
        assert result.exit_code == 2
        assert "unknown embedder provider" in (result.output + (result.stderr or ""))


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

    def test_non_interactive_excludes_openclaw_workspaces(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        # `-y` writes a Claude-Code-only config; OpenClaw is opt-in via wizard.
        home = tmp_path / "home"
        home.mkdir()
        self._seed_agent(home, "ari", with_content=True)
        self._seed_agent(home, "blake", with_content=True)

        vault_path = tmp_path / "vault"
        result = runner.invoke(app, ["init", "-y", "--home", str(home), str(vault_path)])
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["adapters"]["openclaw"]["agent_workspaces"] == []
        assert cfg["adapters"]["openclaw"]["shared_files"] == []

    def test_wizard_prompts_per_agent_opt_in(self, tmp_path: Path, runner: CliRunner) -> None:
        # New behavior: each agent defaults to "no". User opts in by typing "y".
        home = tmp_path / "home"
        home.mkdir()
        self._seed_agent(home, "ari", with_content=True)
        self._seed_agent(home, "blake", with_content=True)

        vault_path = tmp_path / "vault"
        # ari=y, blake=<accept default no>. No shared files / claude prompts to answer.
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="y\n\n",
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        tags = {ws["tag"] for ws in cfg["adapters"]["openclaw"]["agent_workspaces"]}
        assert tags == {"ari"}

    def test_wizard_shared_files_default_no(self, tmp_path: Path, runner: CliRunner) -> None:
        # Shared files default to no — they belong to a workspace, so opt-in too.
        home = tmp_path / "home"
        home.mkdir()
        ws = self._seed_agent(home, "ari", with_content=True)
        (ws / "HARD-RULES.md").write_text("# rules\n")

        vault_path = tmp_path / "vault"
        # All defaults (no for ari, no for HARD-RULES.md).
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="\n\n",
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        assert cfg["adapters"]["openclaw"]["shared_files"] == []

    def test_wizard_can_include_shared_files(self, tmp_path: Path, runner: CliRunner) -> None:
        home = tmp_path / "home"
        home.mkdir()
        ws = self._seed_agent(home, "ari", with_content=True)
        rules = ws / "HARD-RULES.md"
        rules.write_text("# rules\n")

        vault_path = tmp_path / "vault"
        # ari=y, HARD-RULES.md=y.
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="y\ny\n",
        )
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

    def test_wizard_offers_extras_when_workspace_has_them(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        ws = self._seed_agent(home, "ari", with_content=False)
        # Discoverable extras + one item that should be filtered (dated snapshot).
        (ws / "SOUL.md").write_text("# soul\n")
        (ws / "USER.md").write_text("# user\n")
        (ws / "AGENTS_FULL_2026-03-11.md").write_text("# snapshot\n")

        vault_path = tmp_path / "vault"
        # ari=y, extras prompt=<accept default y>.
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="y\n\n",
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        ws_cfg = cfg["adapters"]["openclaw"]["agent_workspaces"][0]
        assert ws_cfg["layout"]["extra_files"] == ["SOUL.md", "USER.md"]

    def test_wizard_can_decline_extras(self, tmp_path: Path, runner: CliRunner) -> None:
        home = tmp_path / "home"
        home.mkdir()
        ws = self._seed_agent(home, "ari", with_content=False)
        (ws / "SOUL.md").write_text("# soul\n")

        vault_path = tmp_path / "vault"
        # ari=y, extras prompt=n.
        result = runner.invoke(
            app,
            ["init", "--home", str(home), str(vault_path)],
            input="y\nn\n",
        )
        assert result.exit_code == 0, result.output
        cfg = yaml.safe_load((vault_path / "_meta" / "config.yaml").read_text())
        ws_cfg = cfg["adapters"]["openclaw"]["agent_workspaces"][0]
        # When declined, layout has no extra_files (default empty list).
        assert ws_cfg.get("layout", {}).get("extra_files", []) == []


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

    def test_reseed_importance_writes_to_frontmatter(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        """`--reseed-importance` fills missing importance values."""
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            mem = _write_memory(vault, idx, title="t", body="x" * 500, type_="memory")
        finally:
            idx.close()
        # Sanity: written memory has no importance yet.
        assert vault.read(mem.path).frontmatter.importance is None

        result = runner.invoke(
            app,
            [
                "reindex",
                "--vault",
                str(initialized_vault),
                "--no-embed",
                "--reseed-importance",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "reseeded importance on 1 record" in result.output

        on_disk = vault.read(mem.path)
        assert on_disk.frontmatter.importance is not None
        assert 0.0 <= on_disk.frontmatter.importance <= 1.0

    def test_reseed_importance_preserves_existing_unless_forced(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            mem = _write_memory(vault, idx, title="t", body="x" * 500, type_="memory")
            # Manually pin the importance to 0.99 so we can tell whether
            # it gets overwritten.
            from memstem.core.frontmatter import Frontmatter

            new_fm: Frontmatter = mem.frontmatter.model_copy(update={"importance": 0.99})
            vault.write(Memory(frontmatter=new_fm, body=mem.body, path=mem.path))
        finally:
            idx.close()

        # Default: --reseed-importance preserves the existing value.
        result = runner.invoke(
            app,
            [
                "reindex",
                "--vault",
                str(initialized_vault),
                "--no-embed",
                "--reseed-importance",
            ],
        )
        assert result.exit_code == 0, result.output
        assert vault.read(mem.path).frontmatter.importance == 0.99

        # --force-reseed overwrites.
        result = runner.invoke(
            app,
            [
                "reindex",
                "--vault",
                str(initialized_vault),
                "--no-embed",
                "--reseed-importance",
                "--force-reseed",
            ],
        )
        assert result.exit_code == 0, result.output
        on_disk = vault.read(mem.path)
        assert on_disk.frontmatter.importance is not None
        assert on_disk.frontmatter.importance != 0.99


class TestHygieneCleanupRetro:
    """W3: retro cleanup CLI."""

    def test_dry_run_reports_no_apply(self, initialized_vault: Path, runner: CliRunner) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="A", body="duplicate body XYZ")
            _write_memory(vault, idx, title="B", body="duplicate body XYZ")
        finally:
            idx.close()

        result = runner.invoke(
            app,
            [
                "hygiene",
                "cleanup-retro",
                "--vault",
                str(initialized_vault),
                "--no-noise",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "RETRO DEDUP PLAN" in result.output
        assert "dry-run" in result.output
        assert "dedup applied" not in result.output

    def test_apply_mutates_vault(self, initialized_vault: Path, runner: CliRunner) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            a = _write_memory(vault, idx, title="A", body="duplicate ZZZ")
            b = _write_memory(vault, idx, title="B", body="duplicate ZZZ")
            # Pin importance values so winner selection is deterministic.
            # We must update both the vault file AND the index row —
            # find_dedup_collisions reads importance from the index.
            from memstem.core.frontmatter import Frontmatter

            new_a_fm: Frontmatter = a.frontmatter.model_copy(update={"importance": 0.9})
            new_a = Memory(frontmatter=new_a_fm, body=a.body, path=a.path)
            vault.write(new_a)
            idx.upsert(new_a)
            new_b_fm: Frontmatter = b.frontmatter.model_copy(update={"importance": 0.4})
            new_b = Memory(frontmatter=new_b_fm, body=b.body, path=b.path)
            vault.write(new_b)
            idx.upsert(new_b)
        finally:
            idx.close()

        result = runner.invoke(
            app,
            [
                "hygiene",
                "cleanup-retro",
                "--vault",
                str(initialized_vault),
                "--no-noise",
                "--apply",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "dedup applied" in result.output
        assert vault.read(a.path).frontmatter.deprecated_by is None
        assert vault.read(b.path).frontmatter.deprecated_by == a.id

    def test_idempotent_apply(self, initialized_vault: Path, runner: CliRunner) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="A", body="dup body for idempotent")
            _write_memory(vault, idx, title="B", body="dup body for idempotent")
        finally:
            idx.close()

        first = runner.invoke(
            app,
            [
                "hygiene",
                "cleanup-retro",
                "--vault",
                str(initialized_vault),
                "--no-noise",
                "--apply",
            ],
        )
        assert first.exit_code == 0
        # Second run: zero collision groups left.
        second = runner.invoke(
            app,
            [
                "hygiene",
                "cleanup-retro",
                "--vault",
                str(initialized_vault),
                "--no-noise",
                "--apply",
            ],
        )
        assert second.exit_code == 0
        assert "Collision groups:        0" in second.output


class TestHygieneVerify:
    """Operator verification report CLI."""

    def test_renders_human_summary(self, initialized_vault: Path, runner: CliRunner) -> None:
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="A", body="alpha body")
            _write_memory(vault, idx, title="B", body="beta body")
        finally:
            idx.close()

        result = runner.invoke(
            app,
            ["hygiene", "verify", "--vault", str(initialized_vault)],
        )
        assert result.exit_code == 0, result.output
        assert "MEMSTEM VERIFY" in result.output
        assert "Total memories:" in result.output
        assert "By type:" in result.output
        assert "Cleanup state:" in result.output
        assert "Derived records:" in result.output

    def test_json_out_writes_machine_readable_payload(
        self, initialized_vault: Path, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The ``--json-out`` flag must produce a parseable JSON payload
        with all the documented fields, so CI / monitoring scrapers can
        depend on the schema."""
        vault = Vault(initialized_vault)
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _write_memory(vault, idx, title="A", body="alpha")
        finally:
            idx.close()

        out_path = tmp_path / "verify.json"
        result = runner.invoke(
            app,
            [
                "hygiene",
                "verify",
                "--vault",
                str(initialized_vault),
                "--json-out",
                str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out_path.is_file()
        payload = json.loads(out_path.read_text())
        for key in (
            "vault_path",
            "total_memories",
            "by_type",
            "deprecated_total",
            "valid_to_total",
            "distilled_session_targets",
            "undistilled_eligible_sessions",
            "active_dedup_groups",
            "active_dedup_skill_groups",
            "active_dedup_to_deprecate",
            "noise_drops",
            "noise_transients",
            "skill_review_tickets",
            "parser_skips",
        ):
            assert key in payload, f"missing key {key!r} in JSON output"


class TestMigrateCommand:
    """Verify the top-level `memstem migrate` command exists and proxies to memstem.migrate."""

    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["migrate", "--help"])
        assert result.exit_code == 0
        assert "flipclaw" in result.output.lower()

    def test_dry_run_default(self, tmp_path: Path, runner: CliRunner) -> None:
        # An empty vault + empty source paths → migrate dry-run should
        # finish cleanly and report 0 records.
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", "-y", "--home", str(empty_home), str(vault)])
        result = runner.invoke(
            app,
            [
                "migrate",
                "--vault",
                str(vault),
                "--openclaw",
                str(empty_home / "no-such-dir"),
                "--claude-root",
                str(empty_home / "no-such-claude"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "DRY-RUN" in result.output
        assert "Re-run with --apply" in result.output

    def test_apply_enqueues_records(self, tmp_path: Path, runner: CliRunner) -> None:
        """Migrate writes records and pushes them onto the embed queue —
        the actual embedding is the worker's job."""
        empty_home = tmp_path / "home"
        ari = empty_home / "ari"
        ari.mkdir(parents=True)
        (ari / "openclaw.json").write_text("{}")
        (ari / "MEMORY.md").write_text("# core\n")
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", "-y", "--home", str(empty_home), str(vault)])

        result = runner.invoke(
            app,
            ["migrate", "--apply", "--vault", str(vault)],
        )
        assert result.exit_code == 0, result.output
        assert "Embed queue" in result.output
        assert "memstem embed" in result.output

    def test_no_embed_flag_is_back_compat_alias(self, tmp_path: Path, runner: CliRunner) -> None:
        """Pre-PR-26 install.sh passes `--no-embed`; we accept it as a no-op."""
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", "-y", "--home", str(empty_home), str(vault)])

        result = runner.invoke(
            app,
            [
                "migrate",
                "--apply",
                "--no-embed",
                "--vault",
                str(vault),
                "--openclaw",
                str(empty_home / "nope"),
                "--claude-root",
                str(empty_home / "nope"),
            ],
        )
        assert result.exit_code == 0, result.output

    def test_apply_mode_label_in_output(self, tmp_path: Path, runner: CliRunner) -> None:
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        vault = tmp_path / "vault"
        runner.invoke(app, ["init", "-y", "--home", str(empty_home), str(vault)])
        result = runner.invoke(
            app,
            [
                "migrate",
                "--apply",
                "--no-embed",
                "--vault",
                str(vault),
                "--openclaw",
                str(empty_home / "nope"),
                "--claude-root",
                str(empty_home / "nope"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "APPLY" in result.output


class TestCommands:
    def test_help_lists_all_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in (
            "init",
            "search",
            "reindex",
            "mcp",
            "daemon",
            "doctor",
            "connect-clients",
            "migrate",
        ):
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
        # Patch the embedder factory so the doctor doesn't talk to a real
        # server. `embed_for` is the function the doctor uses.
        class _StubEmbedder:
            dimensions = 768

            def embed(self, _: str) -> list[float]:
                return [0.0] * 768

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 768 for _ in texts]

            def close(self) -> None: ...

        monkeypatch.setattr("memstem.cli.embed_for", lambda _cfg: _StubEmbedder())
        result = runner.invoke(app, ["doctor", "--vault", str(initialized_vault)])
        assert result.exit_code == 0, result.output
        assert "All checks passed" in result.output
        assert "Python 3" in result.output
        assert "Index opens cleanly" in result.output
        assert "Embed queue" in result.output

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
        # Doctor labels the embedder check by provider+model.
        assert "✗ ollama" in result.output

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

    def test_reports_missing_extra_file(
        self, initialized_vault: Path, tmp_path: Path, runner: CliRunner
    ) -> None:
        ws_root = tmp_path / "agent"
        ws_root.mkdir()
        (ws_root / "MEMORY.md").write_text("# core")
        # Reference an extra that does not exist on disk.
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
            "      - path: " + str(ws_root) + "\n"
            "        tag: agent\n"
            "        layout:\n"
            "          extra_files: [SOUL.md]\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["doctor", "--vault", str(initialized_vault)])
        assert result.exit_code == 1
        assert "OpenClaw extra" in result.output
        assert "file missing" in result.output

    def test_doctor_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Verify the install" in result.output


class TestConnectClients:
    def _vault_with_workspace(self, tmp_path: Path, runner: CliRunner) -> tuple[Path, Path]:
        """Initialize a vault whose config points at a single OpenClaw workspace.

        Returns `(vault, workspace)`.

        Uses the interactive wizard with "y" for the single discovered agent,
        since `init -y` (non-interactive) writes a Claude-Code-only config —
        OpenClaw workspaces are opt-in.
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
        result = runner.invoke(app, ["init", "--home", str(home), str(vault)], input="y\n")
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
        assert data["mcpServers"]["memstem"] == DEFAULT_MCP_SERVER_ENTRY
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


class TestAuth:
    """Tests for `memstem auth set/show/remove`."""

    def _clear_provider_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memstem.auth import PROVIDERS

        for var in PROVIDERS.values():
            monkeypatch.delenv(var, raising=False)

    def test_set_stores_key(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_provider_env(monkeypatch)
        result = runner.invoke(app, ["auth", "set", "openai", "sk-proj-12345abcde"])
        assert result.exit_code == 0, result.output
        assert "stored openai" in result.output
        # Read it back
        from memstem.auth import get_secret

        assert get_secret("openai") == "sk-proj-12345abcde"

    def test_set_unknown_provider_exits_2(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["auth", "set", "bogus", "key"])
        assert result.exit_code == 2
        assert "unknown provider" in result.output

    def test_set_empty_key_exits_2(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        result = runner.invoke(app, ["auth", "set", "openai", "   "])
        assert result.exit_code == 2

    def test_set_reads_from_stdin_when_key_omitted(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        result = runner.invoke(app, ["auth", "set", "voyage"], input="pa-stdin-key\n")
        assert result.exit_code == 0, result.output
        from memstem.auth import get_secret

        assert get_secret("voyage") == "pa-stdin-key"

    def test_show_one_provider_from_file(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        from memstem.auth import set_secret

        set_secret("openai", "sk-proj-abcdef1234567890")
        result = runner.invoke(app, ["auth", "show", "openai"])
        assert result.exit_code == 0
        assert "openai:" in result.output
        assert "(file)" in result.output
        # Mask is in effect: full key not present
        assert "sk-proj-abcdef1234567890" not in result.output

    def test_show_one_provider_from_env(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-1234567890")
        result = runner.invoke(app, ["auth", "show", "openai"])
        assert result.exit_code == 0
        assert "(env: OPENAI_API_KEY)" in result.output

    def test_show_all_providers(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_provider_env(monkeypatch)
        from memstem.auth import set_secret

        set_secret("openai", "sk-openai-1234567890")
        set_secret("voyage", "voyage-key-1234567890")
        result = runner.invoke(app, ["auth", "show"])
        assert result.exit_code == 0
        assert "openai" in result.output
        assert "voyage" in result.output

    def test_show_when_nothing_stored(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        result = runner.invoke(app, ["auth", "show"])
        # Exit 0 — listing nothing isn't an error, but the message must guide
        assert result.exit_code == 0
        assert "memstem auth set" in result.output

    def test_show_unknown_provider_exits_2(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["auth", "show", "bogus"])
        assert result.exit_code == 2

    def test_remove_drops_secret(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_provider_env(monkeypatch)
        from memstem.auth import get_secret, set_secret

        set_secret("openai", "sk-test")
        result = runner.invoke(app, ["auth", "remove", "openai"])
        assert result.exit_code == 0
        assert "removed openai" in result.output
        assert get_secret("openai") is None

    def test_remove_when_not_stored_exits_1(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_provider_env(monkeypatch)
        result = runner.invoke(app, ["auth", "remove", "openai"])
        assert result.exit_code == 1

    def test_remove_unknown_provider_exits_2(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["auth", "remove", "bogus"])
        assert result.exit_code == 2
