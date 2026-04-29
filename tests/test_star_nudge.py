"""Tests for the once-per-machine star nudge."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from memstem import star_nudge


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(star_nudge.ENV_DISABLE, raising=False)
    return tmp_path


class _TTY(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def test_marker_path_uses_xdg(isolated_config: Path) -> None:
    assert star_nudge.marker_path() == isolated_config / "memstem" / ".star-shown"


def test_should_show_returns_true_on_tty_without_marker(isolated_config: Path) -> None:
    assert star_nudge.should_show(stream=_TTY()) is True


def test_should_show_false_when_marker_exists(isolated_config: Path) -> None:
    star_nudge.mark_shown()
    assert star_nudge.should_show(stream=_TTY()) is False


def test_should_show_false_when_env_disabled(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(star_nudge.ENV_DISABLE, "1")
    assert star_nudge.should_show(stream=_TTY()) is False


def test_should_show_false_for_non_tty(isolated_config: Path) -> None:
    assert star_nudge.should_show(stream=io.StringIO()) is False


def test_render_includes_repo_url() -> None:
    text = star_nudge.render()
    assert "github.com/Memstem/memstem" in text
    assert "star" in text.lower()


def test_mark_shown_is_idempotent(isolated_config: Path) -> None:
    star_nudge.mark_shown()
    star_nudge.mark_shown()
    assert star_nudge.marker_path().exists()


def test_maybe_print_writes_marker_after_print(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", _TTY())
    captured: list[str] = []

    star_nudge.maybe_print(captured.append)

    assert captured, "nudge should have been printed"
    assert "github.com/Memstem/memstem" in captured[0]
    assert star_nudge.marker_path().exists()


def test_maybe_print_skips_when_marker_exists(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    star_nudge.mark_shown()
    monkeypatch.setattr(sys, "stdout", _TTY())
    captured: list[str] = []

    star_nudge.maybe_print(captured.append)

    assert captured == []


def test_maybe_print_silent_in_non_tty(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    captured: list[str] = []

    star_nudge.maybe_print(captured.append)

    assert captured == []
    assert not star_nudge.marker_path().exists()
