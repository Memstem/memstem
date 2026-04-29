"""Tests for the daemon HTTP client and CLI daemon-delegation flow.

Covers ADR 0014 Decision 2:

- the discovery contract (`find_daemon` only returns a usable client
  when the daemon is up *and* serves the matching vault),
- the request shape sent to `/search` and the response parsing,
- the CLI search command's three branches: daemon happy path, daemon
  down, and explicit `--no-daemon`.

We use `httpx.MockTransport` to run a fake HTTP server in-process so
the tests are hermetic — they don't require a live `memstem daemon`,
they don't bind to ports, and they don't depend on the network. The
fake transport answers exactly what the real HTTP server returns so
the client's parsing path is exercised end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.client import (
    DaemonClient,
    DaemonError,
    SearchHit,
    find_daemon,
)
from memstem.config import Config, HttpServerConfig
from memstem.core.index import Index

# ---------------------------------------------------------------------------
# Fake daemon helpers — install an httpx.MockTransport on a DaemonClient so
# its requests run against an in-process FastAPI-shaped responder. The
# `responder` callable receives an `httpx.Request` and returns an
# `httpx.Response`, exactly the contract MockTransport expects.
# ---------------------------------------------------------------------------


def _client_with_responder(
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = "http://127.0.0.1:7821",
) -> DaemonClient:
    """Construct a `DaemonClient` whose `httpx.Client` is wired to a
    `MockTransport`. The `DaemonClient.__init__` already builds a real
    httpx.Client, so we replace its underlying transport by swapping
    the client wholesale — the public API stays unchanged."""
    client = DaemonClient(base_url)
    client._client.close()
    client._client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(responder),
    )
    return client


def _ok_health(vault: str) -> Callable[[httpx.Request], httpx.Response]:
    """Responder that answers `/health` with a fixed payload. Anything
    else returns 404 so unintended traffic is loud."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "version": "0.7.0-test",
                    "vault": vault,
                    "embedder": True,
                },
            )
        return httpx.Response(404, json={"detail": f"not mocked: {request.url.path}"})

    return respond


# ---------------------------------------------------------------------------
# DaemonClient.health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_payload_when_up(self, tmp_path: Path) -> None:
        client = _client_with_responder(_ok_health(str(tmp_path / "vault")))
        try:
            health = client.health()
            assert health is not None
            assert health.version == "0.7.0-test"
            assert health.vault == Path(str(tmp_path / "vault"))
            assert health.embedder is True
        finally:
            client.close()

    def test_returns_none_on_connection_refused(self) -> None:
        # Bind to a guaranteed-closed loopback port so connection is
        # refused. Use a port unlikely to be claimed.
        client = DaemonClient("http://127.0.0.1:1")
        try:
            assert client.health(timeout=0.5) is None
        finally:
            client.close()

    def test_returns_none_on_500(self) -> None:
        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        client = _client_with_responder(respond)
        try:
            assert client.health() is None
        finally:
            client.close()

    def test_returns_none_on_malformed_payload(self) -> None:
        def respond(_: httpx.Request) -> httpx.Response:
            # Missing required `version` and `vault` keys.
            return httpx.Response(200, json={"status": "ok"})

        client = _client_with_responder(respond)
        try:
            assert client.health() is None
        finally:
            client.close()


# ---------------------------------------------------------------------------
# DaemonClient.search
# ---------------------------------------------------------------------------


class TestSearch:
    @staticmethod
    def _hit_payload(**overrides: Any) -> dict[str, Any]:
        base = {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Cloudflare doc",
            "type": "memory",
            "snippet": "tunnel etc.",
            "score": 0.0312,
            "path": "memories/cf.md",
            "bm25_rank": 1,
            "vec_rank": 2,
            "frontmatter": {"id": "11111111-1111-4111-8111-111111111111"},
        }
        base.update(overrides)
        return base

    def test_proxies_query_and_parses_response(self) -> None:
        captured: dict[str, Any] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.content.decode()
            return httpx.Response(200, json=[self._hit_payload()])

        client = _client_with_responder(respond)
        try:
            hits = client.search(
                "cloudflare",
                limit=5,
                types=["memory"],
                rrf_k=60,
                bm25_weight=1.0,
                vector_weight=1.0,
                importance_weight=0.2,
            )
        finally:
            client.close()

        assert captured["path"] == "/search"
        assert '"query":"cloudflare"' in captured["body"]
        assert '"limit":5' in captured["body"]
        assert '"types":["memory"]' in captured["body"]
        assert '"importance_weight":0.2' in captured["body"]

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, SearchHit)
        assert hit.id == "11111111-1111-4111-8111-111111111111"
        assert hit.title == "Cloudflare doc"
        assert hit.score == pytest.approx(0.0312)
        assert hit.bm25_rank == 1
        assert hit.vec_rank == 2

    def test_omits_unset_optional_fields(self) -> None:
        """Optional knobs that the caller doesn't pass must not appear in
        the JSON body — otherwise the daemon would override its
        configured defaults with zero/None values."""
        captured: dict[str, Any] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json=[])

        client = _client_with_responder(respond)
        try:
            client.search("q")
        finally:
            client.close()
        assert "rrf_k" not in captured["body"]
        assert "bm25_weight" not in captured["body"]
        assert "vector_weight" not in captured["body"]
        assert "importance_weight" not in captured["body"]
        assert "types" not in captured["body"]

    def test_raises_daemon_error_on_5xx(self) -> None:
        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "embedder offline"})

        client = _client_with_responder(respond)
        try:
            with pytest.raises(DaemonError):
                client.search("q")
        finally:
            client.close()

    def test_raises_daemon_error_on_non_list_payload(self) -> None:
        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        client = _client_with_responder(respond)
        try:
            with pytest.raises(DaemonError):
                client.search("q")
        finally:
            client.close()


# ---------------------------------------------------------------------------
# find_daemon — discovery + vault verification
# ---------------------------------------------------------------------------


class TestFindDaemon:
    def _config(
        self,
        vault: Path,
        *,
        http_enabled: bool = True,
        host: str = "127.0.0.1",
        port: int = 7821,
    ) -> Config:
        return Config(
            vault_path=vault,
            http=HttpServerConfig(enabled=http_enabled, host=host, port=port),
        )

    def test_returns_none_when_http_disabled(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path, http_enabled=False)
        assert find_daemon(cfg) is None

    def test_returns_none_when_unreachable(self, tmp_path: Path) -> None:
        # Port 1 is reserved and refuses connections.
        cfg = self._config(tmp_path, port=1)
        assert find_daemon(cfg, timeout=0.5) is None

    def test_returns_client_when_vault_matches(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        responder = _ok_health(str(tmp_path))

        def fake_factory(base_url: str, *, timeout: float = 30.0) -> DaemonClient:
            return _client_with_responder(responder, base_url=base_url)

        with patch("memstem.client.DaemonClient", side_effect=fake_factory):
            client = find_daemon(cfg)
        assert client is not None
        client.close()

    def test_returns_none_when_vault_mismatches(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        # Daemon reports a *different* vault path. find_daemon must
        # refuse to delegate, otherwise the CLI silently queries the
        # wrong index.
        wrong_vault = tmp_path / "other"
        wrong_vault.mkdir()
        responder = _ok_health(str(wrong_vault))

        def fake_factory(base_url: str, *, timeout: float = 30.0) -> DaemonClient:
            return _client_with_responder(responder, base_url=base_url)

        with patch("memstem.client.DaemonClient", side_effect=fake_factory):
            assert find_daemon(cfg) is None

    def test_resolves_paths_for_vault_comparison(self, tmp_path: Path) -> None:
        """A daemon reporting a relative or symlinked path that resolves
        to the same absolute path the CLI is configured for must still
        match. Otherwise users with `~/memstem-vault` configured one
        way and the daemon another would have search silently fall back
        to direct-DB."""
        cfg = self._config(tmp_path)
        # Different string, same resolved path (use trailing slash and
        # `./` segment to keep the lexical form different).
        responder = _ok_health(f"{tmp_path}/./")

        def fake_factory(base_url: str, *, timeout: float = 30.0) -> DaemonClient:
            return _client_with_responder(responder, base_url=base_url)

        with patch("memstem.client.DaemonClient", side_effect=fake_factory):
            client = find_daemon(cfg)
        assert client is not None
        client.close()


# ---------------------------------------------------------------------------
# CLI integration — `memstem search` daemon delegation
# ---------------------------------------------------------------------------


class TestCliSearchDelegation:
    """End-to-end: the typer CLI under CliRunner with `find_daemon`
    monkey-patched to return either a fake-up client, None, or to
    raise — covering the three branches of the new `search` command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.fixture
    def initialized_vault(self, tmp_path: Path, runner: CliRunner) -> Path:
        from memstem.cli import app as memstem_app

        vault_path = tmp_path / "vault"
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        result = runner.invoke(
            memstem_app,
            ["init", "-y", "--home", str(empty_home), str(vault_path)],
        )
        assert result.exit_code == 0, result.output
        return vault_path

    def test_uses_daemon_when_available(self, initialized_vault: Path, runner: CliRunner) -> None:
        """Happy path: `find_daemon` returns a client; CLI renders its
        results without ever opening the index."""
        captured: dict[str, Any] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search":
                captured["body"] = request.content.decode()
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "11111111-1111-4111-8111-111111111111",
                            "title": "From-daemon hit",
                            "type": "memory",
                            "snippet": "...",
                            "score": 0.5,
                            "path": "memories/x.md",
                            "bm25_rank": 1,
                            "vec_rank": None,
                            "frontmatter": {},
                        }
                    ],
                )
            return _ok_health(str(initialized_vault))(request)

        fake_client = _client_with_responder(respond)

        with (
            patch("memstem.cli.find_daemon", return_value=fake_client),
            patch("memstem.cli._open_index") as opened,
        ):
            result = runner.invoke(
                app,
                ["search", "cloudflare", "--vault", str(initialized_vault)],
            )

        assert result.exit_code == 0, result.output
        assert "From-daemon hit" in result.output
        assert opened.call_count == 0, "daemon path must not open the index"
        assert '"query":"cloudflare"' in captured["body"]

    def test_falls_back_when_daemon_down(self, initialized_vault: Path, runner: CliRunner) -> None:
        """No daemon reachable → `find_daemon` returns None → CLI opens
        the index directly and runs `Search`. Result still renders."""
        # Seed one matching memory via a fresh Index so the direct-DB
        # path has something to find.
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _seed(idx, initialized_vault, "fallback-target", body="cloudflare zone")
        finally:
            idx.close()

        with patch("memstem.cli.find_daemon", return_value=None):
            result = runner.invoke(
                app,
                ["search", "cloudflare", "--vault", str(initialized_vault)],
            )

        assert result.exit_code == 0, result.output
        assert "fallback-target" in result.output

    def test_no_daemon_flag_skips_probe(self, initialized_vault: Path, runner: CliRunner) -> None:
        """`--no-daemon` must short-circuit `find_daemon` entirely so
        debugging the direct-DB path is reliable even when a daemon is
        running on the configured port."""
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _seed(idx, initialized_vault, "direct-only", body="local search")
        finally:
            idx.close()

        with patch("memstem.cli.find_daemon") as probe:
            result = runner.invoke(
                app,
                [
                    "search",
                    "local",
                    "--vault",
                    str(initialized_vault),
                    "--no-daemon",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "direct-only" in result.output
        assert probe.call_count == 0, "--no-daemon must skip the daemon probe"

    def test_falls_back_when_daemon_search_errors(
        self, initialized_vault: Path, runner: CliRunner
    ) -> None:
        """If the daemon answers `/health` but `/search` returns a 5xx,
        the CLI must transparently fall back rather than surface a
        confusing daemon-side error to the user."""
        idx = Index(initialized_vault / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            _seed(idx, initialized_vault, "after-fallback", body="cloudflare doc")
        finally:
            idx.close()

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search":
                return httpx.Response(503, json={"detail": "embedder offline"})
            return _ok_health(str(initialized_vault))(request)

        broken_client = _client_with_responder(respond)
        with patch("memstem.cli.find_daemon", return_value=broken_client):
            result = runner.invoke(
                app,
                ["search", "cloudflare", "--vault", str(initialized_vault)],
            )
        assert result.exit_code == 0, result.output
        assert "after-fallback" in result.output


# ---------------------------------------------------------------------------
# Tiny seeder used by the CLI delegation tests above. Kept local to this
# file so the test_client suite has no dependency on tests/test_cli.py.
# ---------------------------------------------------------------------------


def _seed(
    index: Index,
    vault_path: Path,
    title: str,
    *,
    body: str = "hello world",
) -> None:
    from uuid import uuid4

    from memstem.core.frontmatter import validate
    from memstem.core.storage import Memory, Vault

    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": "memory",
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title,
    }
    fm = validate(metadata)
    memory = Memory(
        frontmatter=fm,
        body=body,
        path=Path("memories") / f"{fm.id}.md",
    )
    Vault(vault_path).write(memory)
    index.upsert(memory)
