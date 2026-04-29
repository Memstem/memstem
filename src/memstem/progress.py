"""Structured phase markers for long-running CLI operations.

ADR 0014 Decision 3: the 35-second `connect()` regression that
produced ADR 0014 was invisible without `py-spy` because the CLI
emitted nothing during the hang. This module makes future hangs of
the same shape visible without external tooling.

Two surfaces:

- ``-v``/``--verbose``: when enabled, every :func:`phase` block
  prints a ``start`` and ``done`` line with elapsed wall-clock time.
  Output goes to stderr so scripts piping CLI stdout aren't affected.

- Always-on slow-op warning: when verbose is *off*, a phase that
  exceeds ``slow_threshold`` seconds prints a single warning to
  stderr. This catches regressions in `connect`, `search`, `embed`,
  etc. for users who never pass `-v`. Default threshold is 2 s,
  comfortable below the threshold where the user starts wondering if
  the CLI is hung.

The helper lives in its own module (rather than inside cli.py) so:

- it is independently testable without spinning up the full typer
  app, and
- non-CLI internal callers (e.g. embed worker batch progress) can
  reuse the same primitive later.

Output format
-------------

Verbose lines:

    [memstem] connect:start
    [memstem] connect:done elapsed=0.02s
    [memstem] search:start
    [memstem] search:done elapsed=0.18s results=5

Slow-op warnings (non-verbose, only when threshold exceeded):

    [memstem] connect took 35.3s -- set --verbose for phase timings
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic as _monotonic
from typing import IO, Any

# `_monotonic` is imported with a private alias so tests can patch
# `memstem.progress._monotonic` without globally replacing
# `time.monotonic` (which is shared with sqlite, asyncio, watchdog,
# etc., and produces flaky test results under load when those
# libraries call into the patched clock).

_PREFIX = "[memstem]"
_DEFAULT_SLOW_THRESHOLD = 2.0
"""Default seconds threshold for the always-on slow-op warning."""


class _ProgressState:
    """Module-level state holder. Encapsulated so tests can reset it
    cleanly via :func:`reset_for_tests` rather than mutating module
    globals directly.

    ``stream`` defaults to ``None`` and is resolved to ``sys.stderr``
    at write time (see :func:`_resolve_stream`). Capturing
    ``sys.stderr`` at class-definition time would freeze the reference
    before frameworks like ``CliRunner`` install their own stderr
    buffer, so verbose output during tests would land on the real
    process stderr instead of the captured one.
    """

    verbose: bool = False
    stream: IO[str] | None = None


_state = _ProgressState()


def _resolve_stream() -> IO[str]:
    """Return the active output stream, lazily falling back to the
    *current* ``sys.stderr`` if no override was set. Lazy resolution
    is what makes ``CliRunner`` capture the verbose output."""
    return _state.stream if _state.stream is not None else sys.stderr


def set_verbose(verbose: bool) -> None:
    """Toggle verbose mode for subsequent :func:`phase` blocks. The
    CLI calls this once after parsing ``-v``/``--verbose``."""
    _state.verbose = bool(verbose)


def is_verbose() -> bool:
    return _state.verbose


def set_stream(stream: IO[str]) -> None:
    """Redirect progress output to ``stream``. Tests use this to
    capture stderr without monkey-patching ``sys.stderr`` globally
    (which fights with pytest's own capture)."""
    _state.stream = stream


def reset_for_tests() -> None:
    """Restore module defaults. Test fixtures call this in teardown
    so leakage across tests is impossible."""
    _state.verbose = False
    _state.stream = None


@contextmanager
def phase(
    name: str,
    *,
    slow_threshold: float = _DEFAULT_SLOW_THRESHOLD,
) -> Iterator[dict[str, Any]]:
    """Wrap a logical phase, printing start/done markers in verbose
    mode and a slow-op warning otherwise.

    Yields a mutable ``details`` dict the caller can stuff with
    metadata (e.g. ``details["results"] = len(hits)``). Each entry is
    rendered as ``key=value`` on the ``done`` line in verbose mode and
    is otherwise ignored.

    The phase is closed in ``finally`` so exceptions inside the block
    still produce timing output — useful when diagnosing failures
    where you want to know *how far* the CLI got before crashing.
    """
    details: dict[str, Any] = {}
    verbose = _state.verbose

    if verbose:
        print(f"{_PREFIX} {name}:start", file=_resolve_stream(), flush=True)

    t0 = _monotonic()
    try:
        yield details
    finally:
        elapsed = _monotonic() - t0
        if verbose:
            tail_parts = [f"{k}={v}" for k, v in details.items()]
            tail = (" " + " ".join(tail_parts)) if tail_parts else ""
            print(
                f"{_PREFIX} {name}:done elapsed={elapsed:.2f}s{tail}",
                file=_resolve_stream(),
                flush=True,
            )
        elif elapsed > slow_threshold:
            print(
                f"{_PREFIX} {name} took {elapsed:.1f}s -- set --verbose for phase timings",
                file=_resolve_stream(),
                flush=True,
            )


__all__ = [
    "is_verbose",
    "phase",
    "reset_for_tests",
    "set_stream",
    "set_verbose",
]
