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
