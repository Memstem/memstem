"""Tests for ``memstem.update_check`` (ADR 0050)."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import memstem
from memstem import update_check as uc


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for name in ("MEMSTEM_NO_UPDATE_CHECK", "MEMSTEM_NO_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(memstem, "__version__", "0.27.0")


def _cfg(**kw: object) -> SimpleNamespace:
    base = {"check": True, "anonymous_stats": True, "endpoint": "https://updates.test/v1/check"}
    base.update(kw)
    return SimpleNamespace(**base)


class _Recorder:
    def __init__(self, *, endpoint_ok: bool = True, latest: str = "0.28.0") -> None:
        self.requests: list[httpx.Request] = []
        self.endpoint_ok = endpoint_ok
        self.latest = latest

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "updates.test":
            if not self.endpoint_ok:
                return httpx.Response(503)
            return httpx.Response(200, json={"latest": self.latest, "released": "2026-10-04"})
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": self.latest}})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_counted_check_sends_only_the_documented_fields() -> None:
    rec = _Recorder()
    status = uc.check_now(_cfg(), client=rec.client())
    assert status is not None and status.update_available and status.via == "updates"
    params = dict(rec.requests[0].url.params)
    assert set(params) == {"v", "os", "py", "src", "id"}
    assert params["v"] == "0.27.0"
    uuid.UUID(params["id"])  # random install id, nothing else
    assert params["os"] in {"linux", "darwin", "windows", "other"}


def test_install_id_is_stable_per_machine() -> None:
    assert uc.install_id() == uc.install_id()


@pytest.mark.parametrize("env", ["DO_NOT_TRACK", "MEMSTEM_NO_TELEMETRY"])
def test_do_not_track_goes_straight_to_pypi(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env, "1")
    rec = _Recorder()
    status = uc.check_now(_cfg(), client=rec.client())
    assert status is not None and status.via == "pypi"
    assert [r.url.host for r in rec.requests] == ["pypi.org"]
    assert not (uc.config_dir() / "install-id").exists()


def test_stats_off_in_config_sends_nothing_identifying() -> None:
    rec = _Recorder()
    uc.check_now(_cfg(anonymous_stats=False), client=rec.client())
    assert [r.url.host for r in rec.requests] == ["pypi.org"]
    assert not rec.requests[0].url.params


def test_check_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    assert uc.check_now(_cfg(check=False), client=rec.client()) is None
    monkeypatch.setenv("MEMSTEM_NO_UPDATE_CHECK", "1")
    assert uc.check_now(_cfg(), client=rec.client()) is None
    assert rec.requests == []


def test_endpoint_down_falls_back_to_pypi() -> None:
    rec = _Recorder(endpoint_ok=False)
    status = uc.check_now(_cfg(), client=rec.client())
    assert status is not None and status.via == "pypi" and status.latest == "0.28.0"


def test_up_to_date_and_cache_roundtrip() -> None:
    rec = _Recorder(latest="0.27.0")
    status = uc.check_now(_cfg(), client=rec.client())
    assert status is not None and not status.update_available
    cached = uc.read_cached()
    assert cached is not None and cached.latest == "0.27.0"
    assert json.loads(uc.cache_path().read_text())["via"] == "updates"


def test_cache_rejudged_after_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    uc.check_now(_cfg(), client=_Recorder(latest="0.28.0").client())
    monkeypatch.setattr(memstem, "__version__", "0.28.0")
    cached = uc.read_cached()
    assert cached is not None and not cached.update_available


@pytest.mark.parametrize(
    ("a", "b", "newer"),
    [
        ("0.28.0", "0.27.0", True),
        ("0.27.0", "0.27.0", False),
        ("0.27.1", "0.28.0", False),
        ("0.30.0", "0.29.9", True),
        (None, "0.27.0", False),
    ],
)
def test_is_newer(a: str | None, b: str, newer: bool) -> None:
    assert uc.is_newer(a, b) is newer


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cli_notice_once_per_version_and_only_on_a_tty() -> None:
    uc.check_now(_cfg(), client=_Recorder(latest="0.28.0").client())
    lines: list[str] = []
    uc.maybe_print_cli_notice(lines.append, stream=io.StringIO())  # pipe: silent
    assert lines == []
    uc.maybe_print_cli_notice(lines.append, stream=_Tty())
    uc.maybe_print_cli_notice(lines.append, stream=_Tty())  # same version: once
    assert len(lines) == 1 and "0.28.0" in lines[0]


def test_disclosure_marker() -> None:
    assert uc.disclosure_pending()
    uc.mark_disclosed()
    assert not uc.disclosure_pending()
    assert "never sends your memories" in uc.DISCLOSURE


def test_health_reports_cached_update_without_degrading(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from memstem.core.index import Index
    from memstem.core.storage import Vault
    from memstem.servers.http_server import build_app

    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    try:
        client = TestClient(build_app(Vault(root), idx))
        assert client.get("/health").json()["update"] is None
        uc.check_now(_cfg(), client=_Recorder(latest="0.28.0").client())
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["update"]["latest"] == "0.28.0" and body["update"]["update_available"]
    finally:
        idx.close()
