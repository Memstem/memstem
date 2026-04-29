"""Once-per-machine GitHub star nudge for memstem CLI commands.

Shown after `memstem init` and after a successful `memstem doctor`. Suppressed
when stdout is not a TTY (so scripts and CI stay clean), when the marker file
already exists (so we never nag the same machine twice), or when
``MEMSTEM_NO_NUDGE`` is set in the environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STAR_URL = "https://github.com/Memstem/memstem"
ENV_DISABLE = "MEMSTEM_NO_NUDGE"


def marker_path() -> Path:
    """Where the once-per-machine marker is stored."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "memstem" / ".star-shown"


def should_show(*, stream: object | None = None) -> bool:
    if os.environ.get(ENV_DISABLE):
        return False
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    return not marker_path().exists()


def mark_shown() -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def render() -> str:
    return (
        "\n"
        "If memstem helps you, please star the repo — it's how I gauge whether to keep building.\n"
        f"  ⭐  {STAR_URL}\n"
    )


def maybe_print(echo: object) -> None:
    """Print the nudge via ``echo`` (typer.echo or print) if appropriate.

    Idempotent and side-effect-light: writes the marker file only after a
    successful print, so a crash in the middle of init/doctor doesn't lose
    the user's one chance to see the nudge.
    """
    if not should_show():
        return
    try:
        echo(render())  # type: ignore[operator]
    except Exception:
        return
    try:
        mark_shown()
    except OSError:
        pass
