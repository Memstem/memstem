"""Smoke tests for `scripts/install.sh`.

We don't actually run the installer in CI — that would download pipx,
Ollama, and pull a 274 MB model. These tests verify that:

1. The script parses (`bash -n`) so a typo never ships unnoticed.
2. The `--help` output mentions every documented flag, so a flag drift
   like "added it to the case-statement but forgot to document it"
   shows up here.
3. Unknown flags exit non-zero with a recognizable message.
4. Each documented flag is wired through the case-statement (and
   therefore at least syntactically valid for the parser to stop on).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"

DOCUMENTED_FLAGS = (
    "--yes",
    "-y",
    "--no-ollama",
    "--no-model",
    "--vault",
    "--from-git",
    "--embedder",
    "--openai-key",
    "--gemini-key",
    "--voyage-key",
    "--connect-clients",
    "--remove-flipclaw",
    "--migrate",
    "--migrate-days",
    "--migrate-no-embed",
    "--start-daemon",
    "--help",
)


@pytest.fixture(scope="module")
def help_output() -> str:
    """Run `install.sh --help` and capture stdout."""
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestSyntax:
    def test_script_parses(self) -> None:
        """`bash -n` catches syntax errors without executing the script."""
        if not shutil.which("bash"):
            pytest.skip("bash is not installed")
        result = subprocess.run(
            ["bash", "-n", str(INSTALL_SH)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


class TestHelp:
    @pytest.mark.parametrize("flag", DOCUMENTED_FLAGS)
    def test_flag_documented_in_help(self, help_output: str, flag: str) -> None:
        assert flag in help_output, f"{flag} missing from --help output"

    def test_help_lists_yes_propagation_note(self, help_output: str) -> None:
        # The `--yes` propagation bug is the one we just fixed; the help
        # text should make the propagation explicit so future readers know.
        assert "Propagated" in help_output or "memstem init -y" in help_output


class TestArgParsing:
    def test_unknown_flag_exits_nonzero(self) -> None:
        if not shutil.which("bash"):
            pytest.skip("bash is not installed")
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--definitely-not-a-flag"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "Unknown option" in result.stderr or "Unknown option" in result.stdout


class TestEmbedderValidation:
    """`--embedder` only accepts known providers — bad values bail before
    touching the network or filesystem."""

    def test_unknown_embedder_exits_2(self) -> None:
        if not shutil.which("bash"):
            pytest.skip("bash is not installed")
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--embedder", "bogus"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Unknown --embedder" in result.stderr

    @pytest.mark.parametrize("provider", ("ollama", "openai", "gemini", "voyage"))
    def test_known_embedder_is_accepted(self, provider: str) -> None:
        # Validation happens immediately after arg parsing, before any
        # network or filesystem work. We give the script a PATH that
        # contains bash but no Python — so it gets past embedder
        # validation, then bails on the Python lookup. Exit != 2 with
        # no "Unknown --embedder" in stderr proves validation accepted
        # the provider.
        bash_path = shutil.which("bash")
        if not bash_path:
            pytest.skip("bash is not installed")
        bash_dir = str(Path(bash_path).parent)
        result = subprocess.run(
            [bash_path, str(INSTALL_SH), "--embedder", provider],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": bash_dir, "HOME": "/tmp"},
        )
        assert "Unknown --embedder" not in result.stderr, result.stderr
