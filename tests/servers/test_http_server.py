"""Tests for the local HTTP server (FastAPI app)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.config import HttpServerConfig, SearchConfig
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.servers.http_server import build_app


def _write_memory(
    vault: Vault,
    index: Index,
    *,
    title: str = "test",
    body: str = "hello world",
) -> Memory:
    fm = validate(
        {
            "id": str(uuid4()),
            "type": "memory",
            "created": "2026-04-25T15:00:00+00:00",
            "updated": "2026-04-25T15:00:00+00:00",
            "source": "human",
            "title": title,
        }
    )
    memory = Memory(frontmatter=fm, body=body, path=Path(f"memories/{fm.id}.md"))
    vault.write(memory)
    index.upsert(memory)
    return memory


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


@pytest.fixture
def client(vault: Vault, index: Index) -> TestClient:
    app = build_app(vault, index, embedder=None, search_config=SearchConfig())
    return TestClient(app)


class TestHealth:
    def test_returns_ok_status(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert body["embedder"] is False  # no embedder passed in

    def test_includes_vault_path(self, client: TestClient, vault: Vault) -> None:
        r = client.get("/health")
        assert r.json()["vault"] == str(vault.root)

    def test_hygiene_block_present(self, client: TestClient) -> None:
        """ADR 0023: /health exposes a hygiene snapshot."""
        body = client.get("/health").json()
        assert "hygiene" in body
        assert body["hygiene"]["loop_enabled"] is True  # default
        assert "last_run" in body["hygiene"]
        # On a fresh vault every stage's last_run is None
        for stage_ts in body["hygiene"]["last_run"].values():
            assert stage_ts is None
        assert body["hygiene"]["running"] == []

    def test_embed_queue_block_and_ok_when_clean(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["problems"] == []
        assert body["embed_queue"]["failed"] == 0
        assert "pending" in body["embed_queue"]

    def test_degraded_on_embed_failures(self, client: TestClient, index: Index) -> None:
        # A permanently-failed embed must flip /health to degraded — the signal
        # that was invisible when status was hardcoded "ok".
        mem_id = "11111111-1111-1111-1111-111111111111"
        index.db.execute(
            """INSERT INTO memories(id, type, source, title, body, path, created, updated)
               VALUES (?, 'memory', 'test', 't', 'b', 'h.md', '2026-01-01', '2026-01-01')""",
            (mem_id,),
        )
        index.db.commit()
        index.enqueue_embed(mem_id)
        for _ in range(6):
            index.mark_embed_error(mem_id, "boom")
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert "embed_failures" in body["problems"]
        assert body["embed_queue"]["failed"] == 1

    def test_hygiene_block_reflects_recorded_run(self, client: TestClient, index: Index) -> None:
        from datetime import UTC, datetime

        from memstem.hygiene.state import STAGE_IMPORTANCE, set_last_run

        ts = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
        set_last_run(index.db, STAGE_IMPORTANCE, ts)
        body = client.get("/health").json()
        assert body["hygiene"]["last_run"][STAGE_IMPORTANCE] == ts.isoformat()


class _StubWatchAdapter(Adapter):
    """Adapter stub with a controllable watcher_alive() for /health tests."""

    def __init__(self, name: str, alive: bool | None) -> None:
        self.name = name
        self._alive = alive

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        return
        yield  # pragma: no cover — makes this an async generator

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        return
        yield  # pragma: no cover

    def watcher_alive(self) -> bool | None:
        return self._alive


class TestWatcherLiveness:
    """B4: /health surfaces per-adapter watchdog observer liveness."""

    @staticmethod
    def _client(vault: Vault, index: Index, adapters: list[Adapter]) -> TestClient:
        return TestClient(build_app(vault, index, adapters=adapters))

    def test_no_adapters_means_empty_block(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["watchers"] == {}
        assert body["status"] == "ok"

    def test_alive_watchers_reported_ok(self, vault: Vault, index: Index) -> None:
        c = self._client(
            vault,
            index,
            [_StubWatchAdapter("openclaw", True), _StubWatchAdapter("claude-code", True)],
        )
        body = c.get("/health").json()
        assert body["watchers"] == {"openclaw": True, "claude-code": True}
        assert body["status"] == "ok"
        assert body["problems"] == []

    def test_dead_watcher_degrades_health(self, vault: Vault, index: Index) -> None:
        c = self._client(
            vault,
            index,
            [_StubWatchAdapter("openclaw", True), _StubWatchAdapter("claude-code", False)],
        )
        body = c.get("/health").json()
        assert body["watchers"]["claude-code"] is False
        assert body["status"] == "degraded"
        assert "watcher_dead:claude-code" in body["problems"]

    def test_not_started_watcher_is_not_a_problem(self, vault: Vault, index: Index) -> None:
        # None = watch() not running (startup, or nothing to observe) —
        # reported for visibility but never degrades.
        c = self._client(vault, index, [_StubWatchAdapter("codex", None)])
        body = c.get("/health").json()
        assert body["watchers"] == {"codex": None}
        assert body["status"] == "ok"


class TestVersion:
    def test_returns_version_string(self, client: TestClient) -> None:
        r = client.get("/version")
        assert r.status_code == 200
        assert "version" in r.json()


class TestSearch:
    def test_finds_match(self, vault: Vault, index: Index, client: TestClient) -> None:
        _write_memory(vault, index, title="Cloudflare guide", body="cloudflare tunnel setup")
        r = client.post("/search", json={"query": "cloudflare", "limit": 5})
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["title"] == "Cloudflare guide"
        assert results[0]["type"] == "memory"
        assert "score" in results[0]
        assert "snippet" in results[0]

    def test_returns_empty_for_no_match(self, client: TestClient) -> None:
        r = client.post("/search", json={"query": "nothing matches"})
        assert r.status_code == 200
        assert r.json() == []

    def test_respects_limit(self, vault: Vault, index: Index, client: TestClient) -> None:
        for i in range(5):
            _write_memory(vault, index, title=f"alpha-{i}", body="alpha topic")
        r = client.post("/search", json={"query": "alpha", "limit": 3})
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_filters_by_type(self, vault: Vault, index: Index, client: TestClient) -> None:
        _write_memory(vault, index, title="memory hit", body="alpha")
        skill_fm = validate(
            {
                "id": str(uuid4()),
                "type": "skill",
                "created": "2026-04-25T15:00:00+00:00",
                "updated": "2026-04-25T15:00:00+00:00",
                "source": "human",
                "title": "skill hit",
                "scope": "universal",
                "verification": "ok",
            }
        )
        skill = Memory(frontmatter=skill_fm, body="alpha", path=Path(f"skills/{skill_fm.id}.md"))
        vault.write(skill)
        index.upsert(skill)

        r = client.post("/search", json={"query": "alpha", "types": ["skill"]})
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["type"] == "skill"

    def test_request_overrides_search_config(
        self, vault: Vault, index: Index, client: TestClient
    ) -> None:
        # Smoke test: weights make it through without erroring.
        _write_memory(vault, index, title="weight test", body="alpha")
        r = client.post(
            "/search",
            json={"query": "alpha", "rrf_k": 30, "bm25_weight": 0.5, "vector_weight": 1.0},
        )
        assert r.status_code == 200

    def test_request_accepts_type_bias_override(
        self, vault: Vault, index: Index, client: TestClient
    ) -> None:
        """The HTTP surface must accept a per-call ``type_bias`` mapping
        and route it through to the underlying ``Search.search`` so an
        operator (or test rig) can disable / override the default
        ranking policy without editing ``_meta/config.yaml``."""
        _write_memory(vault, index, title="type bias test", body="alpha")
        r = client.post(
            "/search",
            json={
                "query": "alpha",
                "type_bias": {"memory": 1.0, "session": 1.0},
            },
        )
        assert r.status_code == 200, r.text
        results = r.json()
        assert isinstance(results, list)


class TestGetMemory:
    def test_by_path(self, vault: Vault, index: Index, client: TestClient) -> None:
        memory = _write_memory(vault, index, title="get by path", body="body text")
        r = client.get(f"/memory/{memory.path}")
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "get by path"
        assert body["body"] == "body text"

    def test_by_id(self, vault: Vault, index: Index, client: TestClient) -> None:
        memory = _write_memory(vault, index, title="get by id")
        r = client.get(f"/memory/{memory.id}")
        assert r.status_code == 200
        assert r.json()["title"] == "get by id"

    def test_404_for_unknown(self, client: TestClient) -> None:
        r = client.get("/memory/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404


class TestBuildApp:
    def test_app_metadata(self, vault: Vault, index: Index) -> None:
        app = build_app(vault, index)
        assert app.title == "Memstem"
        # Routes registered.
        paths = {getattr(route, "path", "") for route in app.routes}
        assert "/health" in paths
        assert "/version" in paths
        assert "/search" in paths


class TestHttpServerConfig:
    def test_defaults(self) -> None:
        c = HttpServerConfig()
        assert c.enabled is True
        assert c.host == "127.0.0.1"
        assert c.port == 7821

    def test_can_disable(self) -> None:
        c = HttpServerConfig(enabled=False)
        assert c.enabled is False

    def test_auth_token_env_default(self) -> None:
        c = HttpServerConfig()
        assert c.auth_token_env == "MEMSTEM_HTTP_TOKEN"


class TestBearerAuth:
    """Optional bearer auth: on when build_app gets a token, off otherwise."""

    @pytest.fixture
    def auth_client(self, vault: Vault, index: Index) -> TestClient:
        app = build_app(vault, index, auth_token="sekrit")
        return TestClient(app)

    def test_no_token_means_open(self, client: TestClient) -> None:
        # The loopback default: no token configured, everything works bare.
        assert client.get("/version").status_code == 200

    def test_missing_header_is_401(self, auth_client: TestClient) -> None:
        assert auth_client.get("/version").status_code == 401
        assert auth_client.post("/search", json={"query": "x"}).status_code == 401
        assert auth_client.get("/memory/anything").status_code == 401

    def test_wrong_token_is_401(self, auth_client: TestClient) -> None:
        r = auth_client.get("/version", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_malformed_scheme_is_401(self, auth_client: TestClient) -> None:
        # Wrong scheme, bare token, and empty value all fail closed.
        # (Non-ASCII header bytes can't be expressed through httpx's
        # TestClient; the middleware compares as bytes so they 401 too.)
        for bad in ("Basic c2VrcmV0", "sekrit", "Bearer", ""):
            r = auth_client.get("/version", headers={"Authorization": bad})
            assert r.status_code == 401, bad

    def test_correct_token_passes(self, auth_client: TestClient) -> None:
        r = auth_client.get("/version", headers={"Authorization": "Bearer sekrit"})
        assert r.status_code == 200

    def test_health_stays_open(self, auth_client: TestClient) -> None:
        # Monitoring must keep working when auth is enabled.
        assert auth_client.get("/health").status_code == 200


class TestRequestClamps:
    """Caller-supplied limit / rerank_top_n are clamped at the edge."""

    def test_huge_limit_clamped(self, vault: Vault, index: Index) -> None:
        from typing import Any

        from memstem.core.search import Search
        from memstem.servers.request_limits import MAX_SEARCH_LIMIT

        _write_memory(vault, index, body="clamp probe")
        seen: dict[str, Any] = {}
        original = Search.search_with_status

        def spy(self: Search, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return original(self, **kwargs)

        app = build_app(vault, index)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Search, "search_with_status", spy)
            client = TestClient(app)
            r = client.post("/search", json={"query": "clamp", "limit": 10**9})
        assert r.status_code == 200
        assert seen["limit"] == MAX_SEARCH_LIMIT

    def test_request_rerank_top_n_clamped(self, vault: Vault, index: Index) -> None:
        from typing import Any

        from memstem.core.search import Search
        from memstem.servers.request_limits import MAX_RERANK_TOP_N

        _write_memory(vault, index, body="clamp probe")
        seen: dict[str, Any] = {}
        original = Search.search_with_status

        def spy(self: Search, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return original(self, **kwargs)

        app = build_app(vault, index)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Search, "search_with_status", spy)
            client = TestClient(app)
            r = client.post("/search", json={"query": "clamp", "rerank_top_n": 10**6})
        assert r.status_code == 200
        assert seen["rerank_top_n"] == MAX_RERANK_TOP_N

    def test_config_default_rerank_not_clamped(self, vault: Vault, index: Index) -> None:
        # Operator-configured defaults are trusted; only request-supplied
        # values get clamped. With no rerank_top_n in the request, whatever
        # effective default the app computed flows through untouched.
        from typing import Any

        from memstem.core.search import Search

        _write_memory(vault, index, body="clamp probe")
        seen: dict[str, Any] = {}
        original = Search.search_with_status

        def spy(self: Search, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return original(self, **kwargs)

        app = build_app(vault, index)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Search, "search_with_status", spy)
            client = TestClient(app)
            r = client.post("/search", json={"query": "clamp"})
        assert r.status_code == 200
        # Reranker disabled in default SearchConfig → effective default is None.
        assert seen["rerank_top_n"] is None


class TestSearchDegradationFlag:
    """ADR 0032 — /search hits carry ``embedder_degraded``."""

    class _BoomEmbedder:
        """Fails like an embedder rejecting a stale key (401)."""

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("401 Unauthorized")

        def embed_query(self, text: str) -> list[float]:
            return self.embed(text)

        def close(self) -> None: ...

    def test_flag_true_when_embedder_fails(self, vault: Vault, index: Index) -> None:
        _write_memory(vault, index, title="hit", body="cloudflare tunnel setup")
        app = build_app(
            vault,
            index,
            embedder=self._BoomEmbedder(),  # type: ignore[arg-type]
            search_config=SearchConfig(),
        )
        r = TestClient(app).post("/search", json={"query": "cloudflare"})
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["embedder_degraded"] is True

    def test_flag_false_without_embedder(
        self, vault: Vault, index: Index, client: TestClient
    ) -> None:
        _write_memory(vault, index, title="hit", body="cloudflare tunnel setup")
        r = client.post("/search", json={"query": "cloudflare"})
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["embedder_degraded"] is False
