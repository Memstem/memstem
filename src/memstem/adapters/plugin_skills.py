"""Discover plugin skills only beneath explicitly approved filesystem roots."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from memstem.config import OpenClawWorkspace

logger = logging.getLogger(__name__)


def iter_plugin_skills(ws: OpenClawWorkspace) -> Iterator[Path]:
    roots = [(ws.path / p).expanduser().resolve() for p in ws.layout.plugin_skill_roots]
    allowed = roots + [
        (ws.path / p).expanduser().resolve() for p in ws.layout.plugin_skill_allowed_roots
    ]
    visited: set[Path] = set()
    stack = sorted(roots, reverse=True)
    while stack:
        original = stack.pop()
        try:
            path = original.resolve(strict=True)
            if not any(path.is_relative_to(root) for root in allowed):
                logger.warning("plugin skill path outside approved roots: %s", original)
                continue
            if path in visited:
                continue
            visited.add(path)
            if path.is_dir():
                stack.extend(sorted(path.iterdir(), reverse=True))
            elif path.name == "SKILL.md" and path.is_file():
                yield path
        except (OSError, RuntimeError) as exc:
            logger.warning("plugin skill path unavailable %s: %s", original, exc)
