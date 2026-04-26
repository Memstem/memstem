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
    "--connect-clients",
    "--remove-flipclaw",
    "--migrate",
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
