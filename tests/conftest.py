"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Create a temporary vault directory with the standard subtree."""
    vault = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


@pytest.fixture(autouse=True)
def _isolate_memstem_secrets(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point every test at a fresh secrets file under pytest's tmp dir.

    Without this, a developer with a real ``~/.config/memstem/secrets.yaml``
    would have tests that pass locally but fail in CI (or vice versa). This
    fixture redirects every test's auth-module reads/writes to a per-session
    tmp file so the suite is hermetic regardless of host state.
    """
    secrets_file = tmp_path_factory.mktemp("memstem-secrets") / "secrets.yaml"
    monkeypatch.setenv("MEMSTEM_SECRETS_FILE", str(secrets_file))
