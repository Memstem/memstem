"""Daily update check and anonymous install count (ADR 0050).

Once a day the daemon asks whether a newer MemStem is out. By default the
question goes to ``updates.memstem.dev``, which answers with the latest PyPI
release and counts the check-in: MemStem version, OS, Python minor version,
install type (PyPI vs source checkout) and a random install ID generated on
this machine. The server also learns the two-letter country Cloudflare
attaches to the request; it never stores the IP address. No memory content,
paths, hostnames or usernames are ever sent. The server code is in
``services/update-server/`` so anyone can verify that.

Opt out of the count (the check then goes straight to PyPI, with nothing
identifying): ``updates.anonymous_stats: false``, ``MEMSTEM_NO_TELEMETRY=1`` or
``DO_NOT_TRACK=1``. Disable the check entirely: ``updates.check: false`` or
``MEMSTEM_NO_UPDATE_CHECK=1``. MemStem never updates itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

import memstem

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://updates.memstem.dev/v1/check"
PYPI_URL = "https://pypi.org/pypi/memstem/json"
CHANGELOG_URL = "https://github.com/Memstem/memstem/blob/main/CHANGELOG.md"
ENV_NO_CHECK = "MEMSTEM_NO_UPDATE_CHECK"
ENV_NO_STATS = ("MEMSTEM_NO_TELEMETRY", "DO_NOT_TRACK")
TIMEOUT_SECONDS = 5.0

DISCLOSURE = (
    "MemStem checks once a day for a new version. With that check it sends an "
    "anonymous count: MemStem version, OS, Python version, install type and a "
    "random install ID (Cloudflare also reports the country; your IP address is "
    "never stored). It never sends your memories, file paths, hostname or "
    "username. Opt out of the count: updates.anonymous_stats: false or "
    "DO_NOT_TRACK=1. Turn the check off: updates.check: false or "
    "MEMSTEM_NO_UPDATE_CHECK=1. Details: "
    "https://github.com/Memstem/memstem/blob/main/docs/privacy.md"
)


@dataclass
class UpdateStatus:
    current: str
    latest: str | None
    update_available: bool
    checked_at: str
    via: str
    """``updates`` (counted check-in) or ``pypi`` (direct, nothing sent)."""
    changelog: str = CHANGELOG_URL
    released: str | None = None


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in ("0", "false", "no", "")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "memstem"


def cache_path() -> Path:
    return config_dir() / "update-check.json"


def checks_enabled(cfg: Any = None) -> bool:
    if _truthy(os.environ.get(ENV_NO_CHECK)):
        return False
    return bool(getattr(cfg, "check", True))


def stats_enabled(cfg: Any = None) -> bool:
    if any(_truthy(os.environ.get(name)) for name in ENV_NO_STATS):
        return False
    return checks_enabled(cfg) and bool(getattr(cfg, "anonymous_stats", True))


def install_id() -> str:
    """Random per-machine ID, created on first use. Not derived from anything."""
    path = config_dir() / "install-id"
    try:
        value = path.read_text().strip()
        uuid.UUID(value)
        return value
    except (OSError, ValueError):
        pass
    value = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    return value


def install_source() -> str:
    """``source`` for editable/VCS installs, ``pypi`` otherwise."""
    try:
        from importlib.metadata import distribution

        raw = distribution("memstem").read_text("direct_url.json")
    except Exception:
        return "pypi"
    if not raw:
        return "pypi"
    try:
        info = json.loads(raw)
    except ValueError:
        return "pypi"
    if info.get("dir_info", {}).get("editable") or "vcs_info" in info:
        return "source"
    return "pypi"


def _os_name() -> str:
    name = platform.system().lower()
    return name if name in ("linux", "darwin", "windows") else "other"


def is_newer(candidate: str | None, current: str | None) -> bool:
    if not candidate or not current:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            return False
    except ImportError:  # pragma: no cover - packaging ships with pip
        import re

        def key(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in re.findall(r"[0-9]+", v.split("+")[0])[:4])

        return key(candidate) > key(current)


def check_now(cfg: Any = None, *, client: httpx.Client | None = None) -> UpdateStatus | None:
    """Run one check. Returns ``None`` when checks are off or both sources fail."""
    if not checks_enabled(cfg):
        return None
    current = memstem.__version__
    owns_client = client is None
    http = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    latest: str | None = None
    released: str | None = None
    via = "pypi"
    try:
        if stats_enabled(cfg):
            endpoint = getattr(cfg, "endpoint", None) or DEFAULT_ENDPOINT
            params = {
                "v": current,
                "os": _os_name(),
                "py": f"{sys.version_info.major}.{sys.version_info.minor}",
                "src": install_source(),
                "id": install_id(),
            }
            try:
                resp = http.get(endpoint, params=params)
                resp.raise_for_status()
                body = resp.json()
                latest, released, via = body.get("latest"), body.get("released"), "updates"
            except Exception as exc:  # endpoint down: fall back to PyPI, send nothing more
                logger.debug("update check endpoint failed (%s); falling back to PyPI", exc)
        if latest is None:
            try:
                resp = http.get(PYPI_URL)
                resp.raise_for_status()
                latest = resp.json()["info"]["version"]
                via = "pypi"
            except Exception as exc:
                logger.debug("update check via PyPI failed: %s", exc)
                return None
    finally:
        if owns_client:
            http.close()
    status = UpdateStatus(
        current=current,
        latest=latest,
        update_available=is_newer(latest, current),
        checked_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        via=via,
        released=released,
    )
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        cache_path().write_text(json.dumps(asdict(status)))
    except OSError:
        pass
    return status


def read_cached() -> UpdateStatus | None:
    try:
        data = json.loads(cache_path().read_text())
        status = UpdateStatus(**data)
    except (OSError, ValueError, TypeError):
        return None
    # The cache can predate an upgrade: re-judge against the running version.
    status.current = memstem.__version__
    status.update_available = is_newer(status.latest, status.current)
    return status


def notice_text(status: UpdateStatus) -> str:
    return (
        f"MemStem {status.latest} is available (you have {status.current}). "
        f"Upgrade: pipx upgrade memstem (or pip install -U memstem). Changes: {status.changelog}"
    )


def _marker(name: str) -> Path:
    return config_dir() / name


def disclosure_pending() -> bool:
    return not _marker(".update-check-disclosed").exists()


def mark_disclosed() -> None:
    try:
        path = _marker(".update-check-disclosed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def maybe_print_cli_notice(echo: Callable[[str], None], *, stream: Any = None) -> None:
    """One line on an interactive terminal, once per new version. Cache only — no network."""
    target = stream if stream is not None else sys.stderr
    isatty = getattr(target, "isatty", None)
    if not callable(isatty) or not isatty() or not checks_enabled():
        return
    status = read_cached()
    if status is None or not status.update_available or not status.latest:
        return
    marker = _marker(f".update-notice-{status.latest}")
    if marker.exists():
        return
    echo(notice_text(status))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        pass


async def run_periodic(cfg: Any, *, initial_delay: float = 120.0) -> None:
    """Daemon task: check after ``initial_delay`` s, then every ``interval_hours``."""
    if not checks_enabled(cfg):
        logger.info("update check disabled")
        return
    if disclosure_pending():
        logger.info(
            "%s",
            DISCLOSURE
            if stats_enabled(cfg)
            else "MemStem checks PyPI once a day for a new version (anonymous count disabled).",
        )
        mark_disclosed()
    interval = max(1, int(getattr(cfg, "interval_hours", 24))) * 3600
    await asyncio.sleep(initial_delay)
    while True:
        status = await asyncio.to_thread(check_now, cfg)
        if status is not None and status.update_available:
            logger.warning("%s", notice_text(status))
        await asyncio.sleep(interval)


__all__ = [
    "DEFAULT_ENDPOINT",
    "DISCLOSURE",
    "UpdateStatus",
    "check_now",
    "checks_enabled",
    "install_id",
    "is_newer",
    "maybe_print_cli_notice",
    "notice_text",
    "read_cached",
    "run_periodic",
    "stats_enabled",
]
