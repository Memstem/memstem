"""Tests for the local HTTP server (FastAPI app)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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
