"""Tests for the structured phase-progress module.

ADR 0014 Decision 3: a 35-second `connect()` regression was invisible
without `py-spy` because the CLI emitted nothing during the hang. The
progress module gives the CLI two diagnostic surfaces:

1. ``-v``/``--verbose``: phase markers to stderr.
2. Always-on slow-op warning: a single line to stderr when a phase
   exceeds the configured threshold even without `-v`.

These tests cover both surfaces directly (without spinning up the
typer CLI) and assert the CLI's `search` command threads them through
correctly.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.index import Index
from memstem.progress import (
    is_verbose,
    phase,
    reset_for_tests,
    set_stream,
    set_verbose,
)


@pytest.fixture(autouse=True)
def _reset_progress_state() -> Iterator[None]:
    """Every test starts with verbose=False and stream=sys.stderr.
    Without this fixture a leaked `set_verbose(True)` from one test
    would silently change behavior in the next."""
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# Verbose mode
# ---------------------------------------------------------------------------


class TestVerboseMode:
    def test_emits_start_and_done_markers(self) -> None:
        buf = io.StringIO()
        set_stream(buf)
        set_verbose(True)

        with phase("connect"):
            pass

        out = buf.getvalue()
        assert "[memstem] connect:start" in out
        assert "[memstem] connect:done" in out
        assert "elapsed=" in out

    def test_includes_caller_supplied_details_on_done_line(self) -> None:
        buf = io.StringIO()
        set_stream(buf)
        set_verbose(True)

        with phase("search") as details:
            details["results"] = 5
            details["fallback"] = False

        out = buf.getvalue()
        # The `done` line should carry both key=value pairs after `elapsed=`.
        done_line = next(line for line in out.splitlines() if "search:done" in line)
        assert "results=5" in done_line
        assert "fallback=False" in done_line

    def test_done_marker_runs_on_exception(self) -> None:
        """If the wrapped block raises, we still want the timing line —
        otherwise diagnosing a crash mid-phase requires reading stack
        traces and timestamping mentally."""
        buf = io.StringIO()
        set_stream(buf)
        set_verbose(True)

        with pytest.raises(RuntimeError):
            with phase("boom"):
                raise RuntimeError("kaboom")

        assert "[memstem] boom:start" in buf.getvalue()
        assert "[memstem] boom:done" in buf.getvalue()


# ---------------------------------------------------------------------------
# Non-verbose mode + slow-op warning
# ---------------------------------------------------------------------------


class TestNonVerboseMode:
    def test_quiet_for_fast_phases(self) -> None:
        buf = io.StringIO()
        set_stream(buf)
        # verbose stays False (default after reset)

        with phase("connect"):
            pass

        assert buf.getvalue() == "", (
            f"non-verbose mode must emit nothing for fast phases; got {buf.getvalue()!r}"
        )

    def test_warns_when_phase_exceeds_slow_threshold(self) -> None:
        """Mock `time.monotonic` so we exercise the threshold logic
        without sleeping. We need monotonically increasing values: the
        first call starts the timer, subsequent calls return larger
        deltas so the elapsed calculation crosses the threshold."""
        buf = io.StringIO()
        set_stream(buf)

        clock = iter([100.0, 105.0])  # 5 seconds elapsed
        with patch("memstem.progress._monotonic", side_effect=lambda: next(clock)):
            with phase("connect", slow_threshold=2.0):
                pass

        out = buf.getvalue()
        assert "[memstem] connect took 5.0s" in out
        assert "set --verbose" in out

    def test_does_not_warn_below_threshold(self) -> None:
        buf = io.StringIO()
        set_stream(buf)

        clock = iter([100.0, 100.5])  # 0.5 s — under default 2.0 threshold
        with patch("memstem.progress._monotonic", side_effect=lambda: next(clock)):
            with phase("connect"):
                pass

        assert buf.getvalue() == ""

    def test_threshold_override_per_phase(self) -> None:
        """Callers can pass a tighter or looser threshold for a
        specific phase (e.g. a daemon health probe is expected to be
        sub-second; a reindex might run for minutes)."""
        buf = io.StringIO()
        set_stream(buf)

        clock = iter([100.0, 100.6])
        with patch("memstem.progress._monotonic", side_effect=lambda: next(clock)):
            with phase("daemon-probe", slow_threshold=0.5):
                pass

        assert "daemon-probe took 0.6s" in buf.getvalue()


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestStateHelpers:
    def test_set_verbose_toggles(self) -> None:
        assert is_verbose() is False
        set_verbose(True)
        assert is_verbose() is True
        set_verbose(False)
        assert is_verbose() is False

    def test_reset_for_tests_clears_verbose(self) -> None:
        set_verbose(True)
        reset_for_tests()
        assert is_verbose() is False


# ---------------------------------------------------------------------------
# CLI integration — `memstem search -v` threads phase markers through.
# We use the real CLI under CliRunner with a hermetic vault and patched
# `find_daemon` so no network is involved.
# ---------------------------------------------------------------------------


class TestCliSearchVerbose:
    @pytest.fixture
    def runner(self) -> CliRunner:
        # CliRunner separates stdout and stderr by default in click 8.x,
        # so `result.stderr` carries the phase markers while
        # `result.stdout` carries the search results. Lazy resolution
        # of `sys.stderr` inside `progress.phase()` is what lets the
        # runner's captured stream see the output (see
        # `_resolve_stream` in progress.py).
        return CliRunner()

    @pytest.fixture
    def initialized_vault(self, tmp_path: Path, runner: CliRunner) -> Path:
        vault_path = tmp_path / "vault"
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        result = runner.invoke(
            app,
            ["init", "-y", "--home", str(empty_home), str(vault_path)],
        )
        assert result.exit_code == 0, result.stdout
        return vault_path

    def test_verbose_flag_emits_markers_to_stderr(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        with patch("memstem.cli.find_daemon", return_value=None):
            result = runner.invoke(
                app,
                [
                    "search",
                    "anything",
                    "--vault",
                    str(initialized_vault),
                    "--no-daemon",
                    "-v",
                ],
            )
        assert result.exit_code == 0, result.stdout
        # `--no-daemon` skips the daemon-probe phase entirely; we get
        # the outer search wrapper plus the connect + direct-search
        # phases.
        assert "[memstem] search:start" in result.stderr
        assert "[memstem] connect:done" in result.stderr
        assert "[memstem] direct-search:done" in result.stderr
        assert "[memstem] search:done" in result.stderr

    def test_default_mode_quiet_on_stderr(self, initialized_vault: Path, runner: CliRunner) -> None:
        """Without `-v`, the CLI must not emit phase markers to stderr
        for fast operations — a noisy default would bury actual warnings
        from logging."""
        with patch("memstem.cli.find_daemon", return_value=None):
            result = runner.invoke(
                app,
                [
                    "search",
                    "anything",
                    "--vault",
                    str(initialized_vault),
                    "--no-daemon",
                ],
            )
        assert result.exit_code == 0, result.stdout
        assert "[memstem]" not in result.stderr, (
            f"non-verbose CLI emitted phase markers; stderr was {result.stderr!r}"
        )

    def test_verbose_search_with_daemon_emits_daemon_markers(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        """The daemon-search code path produces its own marker
        (`daemon-search:done results=N`) that the direct-DB path does
        not. Verifies both transports are wrapped consistently."""
        import httpx

        from memstem.client import DaemonClient

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search":
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        client = DaemonClient("http://127.0.0.1:7821")
        client._client.close()
        client._client = httpx.Client(
            base_url="http://127.0.0.1:7821",
            transport=httpx.MockTransport(respond),
        )

        with patch("memstem.cli.find_daemon", return_value=client):
            result = runner.invoke(
                app,
                ["search", "anything", "--vault", str(initialized_vault), "-v"],
            )
        assert result.exit_code == 0, result.stdout
        assert "[memstem] daemon-probe:done" in result.stderr
        assert "found=True" in result.stderr
        assert "[memstem] daemon-search:done" in result.stderr
        assert "results=0" in result.stderr


# ---------------------------------------------------------------------------
# Slow-op warning lands on real-world `_open_index` regressions even
# without `-v`. The CLI's direct-DB path wraps `_open_index` in
# `phase("connect")`, so a slow connect will produce the warning. We
# patch the wrapped function to simulate slowness rather than constructing
# a 1+ GB fixture.
# ---------------------------------------------------------------------------


class TestCliConnectSlowOpWarning:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.fixture
    def initialized_vault(self, tmp_path: Path, runner: CliRunner) -> Path:
        vault_path = tmp_path / "vault"
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        result = runner.invoke(
            app,
            ["init", "-y", "--home", str(empty_home), str(vault_path)],
        )
        assert result.exit_code == 0
        return vault_path

    def test_slow_connect_emits_warning_without_verbose(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        """Patches the progress module's clock so the outer `search`
        phase appears to take >2 seconds. The CLI must surface this to
        stderr even without `-v` — that's how a future connect-cost
        regression becomes visible to the user.

        The patch targets `memstem.progress._monotonic` (the private
        alias the module imports from `time.monotonic`) rather than
        `time.monotonic` itself. Patching the global clock would also
        intercept calls from sqlite/asyncio/watchdog inside the same
        test process, which under load skews `_monotonic` results
        unpredictably and produces order-dependent test failures.
        """
        call_index = [0]

        def fake_monotonic() -> float:
            call_index[0] += 1
            # First call (the outer `search:start` t0) returns 0.
            # Every subsequent call returns 5 — so the outer phase's
            # exit measures 5 s elapsed and triggers the warning.
            # Inner phases enter and exit at the same flat 5, so they
            # measure ~0 s and don't fire (which keeps the test
            # asserting the *outer* warning, not arbitrary phases).
            return 5.0 if call_index[0] >= 2 else 0.0

        with (
            patch("memstem.progress._monotonic", side_effect=fake_monotonic),
            patch("memstem.cli.find_daemon", return_value=None),
        ):
            result = runner.invoke(
                app,
                [
                    "search",
                    "anything",
                    "--vault",
                    str(initialized_vault),
                    "--no-daemon",
                ],
            )

        assert result.exit_code == 0, result.stdout
        # Some phase warning should have fired. Exact phase name varies
        # depending on which call happened to land on the 5s delta;
        # what matters is the user-visible "took ... seconds" line.
        assert "took" in result.stderr
        assert "set --verbose" in result.stderr


# ---------------------------------------------------------------------------
# Sanity check: the slow-op warning never fires on a fast connect against
# a real index — we don't want to scare CI logs with phantom warnings.
# ---------------------------------------------------------------------------


def test_real_connect_does_not_trigger_slow_warning(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    buf = io.StringIO()
    set_stream(buf)

    idx = Index(db_path, dimensions=8)
    try:
        with phase("connect", slow_threshold=2.0):
            idx.connect()
    finally:
        idx.close()

    assert buf.getvalue() == "", (
        "fast Index.connect() unexpectedly produced a slow-op warning; "
        f"stderr was {buf.getvalue()!r}"
    )
