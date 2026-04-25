#!/usr/bin/env python3
"""One-shot migration from FlipClaw / Ari into a Memstem vault.

Thin wrapper that delegates to `memstem.migrate.app`. See the module
docstring there for behavior details.
"""

from __future__ import annotations

import logging

from memstem.migrate import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


if __name__ == "__main__":
    app()
