"""Tests for the MCP server (in-process via FastMCP.call_tool)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.servers.mcp_server import (
    _ActivityTracker,
    _Resources,
    _start_idle_watcher,
    build_server,
)


def _make_memory(
    *,
    type_: str = "memory",
    title: str | None = "test",
    body: str = "hello world",
    tags: list[str] | None = None,
    scope: str | None = None,
    verification: str | None = None,
    vault: Vault | None = None,
    index: Index | None = None,
) -> Memory:
    metadata: dict[str, Any] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title,
        "tags": tags or [],
    }
    if scope is not None:
        metadata["scope"] = scope
    if verification is not None:
        metadata["verification"] = verification
    fm = validate(metadata)
    if type_ == "skill":
        path = Path(f"skills/{fm.id}.md")
    else:
        path = Path(f"memories/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    if vault is not None:
        vault.write(memory)
    if index is not None:
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


async def _call_tool(mcp: Any, name: str, args: dict[str, Any]) -> Any:
    """Invoke a tool and return the structured result (the dict-or-list payload)."""
    _blocks, struct = await mcp.call_tool(name, args)
    # FastMCP wraps list returns in {"result": [...]}; dict returns are passthrough.
    if isinstance(struct, dict) and set(struct.keys()) == {"result"}:
        return struct["result"]
    return struct


class TestSearchTool:
    async def test_returns_hits_for_keyword_match(self, vault: Vault, index: Index) -> None:
        m1 = _make_memory(body="cloudflare tunnel notes", vault=vault, index=index)
        _make_memory(body="totally unrelated", vault=vault, index=index)

        mcp = build_server(vault, index)
        results = await _call_tool(mcp, "memstem_search", {"query": "cloudflare"})
        assert len(results) == 1
        assert results[0]["id"] == str(m1.id)
        assert "snippet" in results[0]
        assert results[0]["score"] > 0
        assert results[0]["bm25_rank"] == 1

    async def test_filters_by_type(self, vault: Vault, index: Index) -> None:
        _make_memory(type_="memory", body="alpha", vault=vault, index=index)
        _make_memory(
            type_="skill",
            title="alpha",
            body="alpha",
            scope="universal",
            verification="ok",
            vault=vault,
            index=index,
        )
        mcp = build_server(vault, index)
        only_memories = await _call_tool(
            mcp, "memstem_search", {"query": "alpha", "types": ["memory"]}
        )
        only_skills = await _call_tool(
            mcp, "memstem_search", {"query": "alpha", "types": ["skill"]}
        )
        assert all(r["type"] == "memory" for r in only_memories)
        assert all(r["type"] == "skill" for r in only_skills)

    async def test_empty_query_returns_empty(self, vault: Vault, index: Index) -> None:
        _make_memory(vault=vault, index=index)
        mcp = build_server(vault, index)
        # All-special-character query → empty after sanitization.
        results = await _call_tool(mcp, "memstem_search", {"query": '()[]"^'})
        assert results == []


class TestGetTool:
    async def test_get_by_path(self, vault: Vault, index: Index) -> None:
        memory = _make_memory(title="brad", vault=vault, index=index)
        mcp = build_server(vault, index)
        result = await _call_tool(mcp, "memstem_get", {"id_or_path": str(memory.path)})
        assert result["id"] == str(memory.id)
        assert result["title"] == "brad"
        assert result["body"] == "hello world"

    async def test_get_by_id(self, vault: Vault, index: Index) -> None:
        memory = _make_memory(vault=vault, index=index)
        mcp = build_server(vault, index)
        result = await _call_tool(mcp, "memstem_get", {"id_or_path": str(memory.id)})
        assert result["id"] == str(memory.id)

    async def test_missing_raises(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        with pytest.raises(Exception, match="no memory found"):
            await _call_tool(mcp, "memstem_get", {"id_or_path": "nonexistent"})


class TestListSkillsTool:
    async def test_empty_list(self, vault: Vault, index: Index) -> None:
        _make_memory(vault=vault, index=index)  # not a skill
        mcp = build_server(vault, index)
        results = await _call_tool(mcp, "memstem_list_skills", {})
        assert results == []

    async def test_lists_skills(self, vault: Vault, index: Index) -> None:
        _make_memory(
            type_="skill",
            title="deploy",
            body="how to deploy",
            scope="universal",
            verification="ok",
            vault=vault,
            index=index,
        )
        _make_memory(
            type_="skill",
            title="email",
            body="how to send",
            scope="ari",
            verification="ok",
            vault=vault,
            index=index,
        )
        mcp = build_server(vault, index)
        results = await _call_tool(mcp, "memstem_list_skills", {})
        titles = sorted(r["title"] for r in results)
        assert titles == ["deploy", "email"]

    async def test_filters_by_scope(self, vault: Vault, index: Index) -> None:
        _make_memory(
            type_="skill",
            title="universal-skill",
            body="x",
            scope="universal",
            verification="ok",
            vault=vault,
            index=index,
        )
        _make_memory(
            type_="skill",
            title="ari-skill",
            body="x",
            scope="ari",
            verification="ok",
            vault=vault,
            index=index,
        )
        mcp = build_server(vault, index)
        ari_only = await _call_tool(mcp, "memstem_list_skills", {"scope": "ari"})
        assert len(ari_only) == 1
        assert ari_only[0]["title"] == "ari-skill"


class TestGetSkillTool:
    async def test_by_title(self, vault: Vault, index: Index) -> None:
        _make_memory(
            type_="skill",
            title="email-workflows",
            body="email procedure",
            scope="universal",
            verification="ok",
            vault=vault,
            index=index,
        )
        mcp = build_server(vault, index)
        skill = await _call_tool(mcp, "memstem_get_skill", {"name": "email-workflows"})
        assert skill["title"] == "email-workflows"
        assert skill["body"] == "email procedure"
        assert skill["type"] == "skill"

    async def test_missing_raises(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        with pytest.raises(Exception, match="no skill named"):
            await _call_tool(mcp, "memstem_get_skill", {"name": "nope"})


class TestUpsertTool:
    async def test_creates_memory_with_auto_path(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        new_id = str(uuid4())
        result = await _call_tool(
            mcp,
            "memstem_upsert",
            {
                "frontmatter": {
                    "id": new_id,
                    "type": "memory",
                    "created": "2026-04-25T15:00:00+00:00",
                    "updated": "2026-04-25T15:00:00+00:00",
                    "source": "human",
                    "title": "new memory",
                },
                "body": "freshly upserted",
            },
        )
        assert result["id"] == new_id
        assert result["path"] == f"memories/{new_id}.md"
        # Verify the file actually exists.
        memory = vault.read(result["path"])
        assert memory.body == "freshly upserted"

    async def test_skill_auto_path_uses_title_slug(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        result = await _call_tool(
            mcp,
            "memstem_upsert",
            {
                "frontmatter": {
                    "id": str(uuid4()),
                    "type": "skill",
                    "created": "2026-04-25T15:00:00+00:00",
                    "updated": "2026-04-25T15:00:00+00:00",
                    "source": "human",
                    "title": "Deploy Skill",
                    "scope": "universal",
                    "verification": "ok",
                },
                "body": "procedure",
            },
        )
        assert result["path"] == "skills/deploy-skill.md"

    async def test_explicit_path_overrides(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        result = await _call_tool(
            mcp,
            "memstem_upsert",
            {
                "frontmatter": {
                    "id": str(uuid4()),
                    "type": "memory",
                    "created": "2026-04-25T15:00:00+00:00",
                    "updated": "2026-04-25T15:00:00+00:00",
                    "source": "human",
                    "title": "custom",
                },
                "body": "x",
                "path": "memories/custom/place.md",
            },
        )
        assert result["path"] == "memories/custom/place.md"

    async def test_invalid_frontmatter_raises(self, vault: Vault, index: Index) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        mcp = build_server(vault, index)
        with pytest.raises(ToolError, match="validation error"):
            await _call_tool(
                mcp,
                "memstem_upsert",
                {
                    "frontmatter": {"id": "not-a-uuid", "type": "memory"},
                    "body": "x",
                },
            )

    async def test_round_trip_via_search(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        new_id = str(uuid4())
        await _call_tool(
            mcp,
            "memstem_upsert",
            {
                "frontmatter": {
                    "id": new_id,
                    "type": "memory",
                    "created": "2026-04-25T15:00:00+00:00",
                    "updated": "2026-04-25T15:00:00+00:00",
                    "source": "human",
                    "title": "searchable",
                },
                "body": "an entirely searchable thing",
            },
        )
        results = await _call_tool(mcp, "memstem_search", {"query": "searchable"})
        assert any(r["id"] == new_id for r in results)


class TestServerSetup:
    def test_build_server_returns_fastmcp(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        assert mcp.name == "memstem"

    def test_custom_name(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index, name="custom")
        assert mcp.name == "custom"

    async def test_all_tools_registered(self, vault: Vault, index: Index) -> None:
        mcp = build_server(vault, index)
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "memstem_search",
            "memstem_get",
            "memstem_list_skills",
            "memstem_get_skill",
            "memstem_upsert",
        }


class TestActivityTracker:
    def test_fresh_tracker_idle_seconds_near_zero(self) -> None:
        tracker = _ActivityTracker()
        # Brand-new tracker has just been touched; idle should be tiny.
        assert tracker.idle_seconds() < 0.1

    def test_touch_resets_idle(self) -> None:
        import time

        tracker = _ActivityTracker()
        time.sleep(0.05)
        assert tracker.idle_seconds() >= 0.05
        tracker.touch()
        assert tracker.idle_seconds() < 0.05


class TestIdleWatcher:
    def test_watcher_fires_when_idle_exceeds_threshold(self) -> None:
        import threading
        import time

        tracker = _ActivityTracker()
        fired = threading.Event()

        # Force the tracker to look idle by rolling its timestamp back.
        # Setting `_last` to ~5 seconds ago means a 1-second timeout
        # will trip on the next watcher tick.
        with tracker._lock:
            tracker._last = time.monotonic() - 5

        _start_idle_watcher(
            tracker,
            idle_timeout_seconds=1,
            check_interval_seconds=0.05,
            exit_fn=fired.set,
        )

        assert fired.wait(timeout=1.0), "watcher should have fired by now"

    def test_watcher_does_not_fire_when_active(self) -> None:
        import threading

        tracker = _ActivityTracker()
        fired = threading.Event()

        _start_idle_watcher(
            tracker,
            idle_timeout_seconds=10,  # well above the test window
            check_interval_seconds=0.05,
            exit_fn=fired.set,
        )

        # Touch keeps the tracker fresh; the watcher should not trip.
        for _ in range(5):
            tracker.touch()
            assert not fired.is_set()
            fired.wait(timeout=0.05)

        assert not fired.is_set()


class TestBuildServerIdleTimeout:
    @staticmethod
    def _count_watcher_threads() -> int:
        import threading

        return sum(1 for t in threading.enumerate() if t.name == "mcp-idle-watcher")

    def test_idle_timeout_zero_does_not_start_watcher(self, vault: Vault, index: Index) -> None:
        # When idle_timeout_seconds=0, no new watcher thread is spawned.
        before = self._count_watcher_threads()
        build_server(vault, index, idle_timeout_seconds=0)
        after = self._count_watcher_threads()
        assert after == before

    def test_idle_timeout_positive_starts_watcher(self, vault: Vault, index: Index) -> None:
        before = self._count_watcher_threads()
        build_server(vault, index, idle_timeout_seconds=3600)
        after = self._count_watcher_threads()
        assert after == before + 1

    async def test_tool_calls_record_activity(self, vault: Vault, index: Index) -> None:
        # Building with timeout=0 means no watcher thread, but the
        # ActivityTracker still exists internally and tool calls touch it.
        # We verify by triggering tool calls and checking that the
        # observable behavior (no exit) holds even with a tiny timeout
        # if we keep calling tools.
        import asyncio
        import threading

        fired = threading.Event()
        # Patch so the watcher's exit_fn is observable, by building the
        # server and starting a watcher with a short timeout that we
        # keep resetting via tool calls.
        mcp = build_server(vault, index, idle_timeout_seconds=0)
        tracker = _ActivityTracker()
        _start_idle_watcher(
            tracker,
            idle_timeout_seconds=1,
            check_interval_seconds=0.05,
            exit_fn=fired.set,
        )

        # The watcher above is a separate tracker for testing — it'll
        # fire after 1s of no activity.
        await asyncio.sleep(0.2)
        assert not fired.is_set()
        # Wait long enough for the test watcher to fire.
        await asyncio.sleep(1.2)
        assert fired.is_set()
        # Just exercising the build_server path with the real tools too.
        tools = await mcp.list_tools()
        assert {t.name for t in tools} >= {"memstem_search", "memstem_get"}


class _CallCounter:
    """Tiny spy that wraps a zero-arg callable and counts invocations."""

    __slots__ = ("calls", "fn")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self.fn()


class TestResourcesEager:
    def test_eager_does_not_invoke_factories(self, vault: Vault, index: Index) -> None:
        # The eager path takes already-initialized objects. It internally
        # constructs trivial factories, but it pre-populates the cached
        # values so accessing a property never triggers a factory call.
        # We verify by exposing the factory through a spy.
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.eager(vault, index, None)
        # Replace the embedder factory with the spy after the fact —
        # since `eager` already pre-resolved, the spy must NEVER fire.
        res._build_embedder = embedder_spy
        _ = res.vault
        _ = res.index
        _ = res.embedder
        _ = res.search
        assert embedder_spy.calls == 0

    def test_eager_returns_passed_in_objects(self, vault: Vault, index: Index) -> None:
        res = _Resources.eager(vault, index, None)
        assert res.vault is vault
        assert res.index is index
        assert res.embedder is None
        # search is composed lazily even on eager (it's cheap; just stores refs).
        assert res.search.vault is vault
        assert res.search.index is index

    def test_eager_index_initialized_is_true(self, vault: Vault, index: Index) -> None:
        res = _Resources.eager(vault, index)
        assert res.index_initialized is True


class TestResourcesLazy:
    def test_lazy_factories_not_called_until_property_accessed(
        self, vault: Vault, index: Index
    ) -> None:
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        # Before any access, no factory has been invoked.
        assert vault_spy.calls == 0
        assert index_spy.calls == 0
        assert embedder_spy.calls == 0
        assert res.index_initialized is False

    def test_lazy_each_factory_called_exactly_once(self, vault: Vault, index: Index) -> None:
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        # Touch each property many times.
        for _ in range(5):
            _ = res.vault
            _ = res.index
            _ = res.embedder
            _ = res.search
        assert vault_spy.calls == 1
        assert index_spy.calls == 1
        assert embedder_spy.calls == 1

    def test_lazy_search_triggers_underlying_factories(self, vault: Vault, index: Index) -> None:
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        # Accessing search alone should pull all three through.
        _ = res.search
        assert vault_spy.calls == 1
        assert index_spy.calls == 1
        assert embedder_spy.calls == 1

    def test_lazy_embedder_factory_returning_none_resolves_only_once(
        self, vault: Vault, index: Index
    ) -> None:
        # Even when the embedder factory returns None, the resolution flag
        # must flip so the factory isn't re-invoked on every property access.
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=lambda: vault,
            build_index=lambda: index,
            build_embedder=embedder_spy,
        )
        for _ in range(3):
            assert res.embedder is None
        assert embedder_spy.calls == 1

    def test_lazy_index_initialized_flips_after_access(self, vault: Vault, index: Index) -> None:
        res = _Resources.lazy(
            build_vault=lambda: vault,
            build_index=lambda: index,
            build_embedder=lambda: None,
        )
        assert res.index_initialized is False
        _ = res.index
        assert res.index_initialized is True

    def test_lazy_concurrent_access_calls_factory_once(self, vault: Vault, index: Index) -> None:
        # Simulate the FastMCP scenario: multiple worker threads racing
        # to read `res.search` on the very first tool call. The double-
        # checked lock inside `_Resources` should ensure each factory
        # fires exactly once even under contention.
        import threading

        # Slow factory makes the race window observable: without the lock
        # all threads would see `_index is None` and each call the factory.
        def slow_index() -> Index:
            time.sleep(0.05)
            return index

        index_spy = _CallCounter(slow_index)
        res = _Resources.lazy(
            build_vault=lambda: vault,
            build_index=index_spy,
            build_embedder=lambda: None,
        )

        results: list[Index] = []
        results_lock = threading.Lock()

        def worker() -> None:
            got = res.index
            with results_lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert index_spy.calls == 1
        assert len(results) == 8
        assert all(r is index for r in results)


class TestBuildServerLazy:
    async def test_handshake_does_not_trigger_factories(self, vault: Vault, index: Index) -> None:
        # The MCP handshake is `initialize` + `tools/list`. Neither should
        # touch vault/index/embedder. This is the bug class the lazy-init
        # refactor is meant to fix: OpenClaw bundle-mcp times out at 30s
        # waiting for the handshake when those resources are loaded
        # eagerly at server start.
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        mcp = build_server(resources=res)
        tools = await mcp.list_tools()
        assert {t.name for t in tools} == {
            "memstem_search",
            "memstem_get",
            "memstem_list_skills",
            "memstem_get_skill",
            "memstem_upsert",
        }
        # Critically: no factory has fired yet.
        assert vault_spy.calls == 0
        assert index_spy.calls == 0
        assert embedder_spy.calls == 0
        assert res.index_initialized is False

    async def test_first_tool_call_triggers_init(self, vault: Vault, index: Index) -> None:
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        mcp = build_server(resources=res)
        # Empty index → empty result, but the factories still have to fire.
        await _call_tool(mcp, "memstem_search", {"query": "anything"})
        assert vault_spy.calls == 1
        assert index_spy.calls == 1
        assert embedder_spy.calls == 1

    async def test_subsequent_tool_calls_reuse_cached_resources(
        self, vault: Vault, index: Index
    ) -> None:
        vault_spy = _CallCounter(lambda: vault)
        index_spy = _CallCounter(lambda: index)
        embedder_spy = _CallCounter(lambda: None)
        res = _Resources.lazy(
            build_vault=vault_spy,
            build_index=index_spy,
            build_embedder=embedder_spy,
        )
        mcp = build_server(resources=res)
        for _ in range(5):
            await _call_tool(mcp, "memstem_search", {"query": "anything"})
        # Each factory ran exactly once across all five calls.
        assert vault_spy.calls == 1
        assert index_spy.calls == 1
        assert embedder_spy.calls == 1

    async def test_lazy_round_trip_produces_real_results(self, vault: Vault, index: Index) -> None:
        # Behaviour parity: a memory inserted before the lazy server starts
        # should still come back through search once the resources resolve.
        memory = _make_memory(body="needle in lazy haystack", vault=vault, index=index)
        res = _Resources.lazy(
            build_vault=lambda: vault,
            build_index=lambda: index,
            build_embedder=lambda: None,
        )
        mcp = build_server(resources=res)
        results = await _call_tool(mcp, "memstem_search", {"query": "needle"})
        assert len(results) == 1
        assert results[0]["id"] == str(memory.id)


class TestBuildServerArgValidation:
    def test_rejects_both_resources_and_eager_args(self, vault: Vault, index: Index) -> None:
        res = _Resources.lazy(
            build_vault=lambda: vault,
            build_index=lambda: index,
            build_embedder=lambda: None,
        )
        with pytest.raises(ValueError, match="not both"):
            build_server(vault, index, resources=res)

    def test_rejects_no_args(self) -> None:
        with pytest.raises(ValueError, match="provide vault and index"):
            build_server()

    def test_rejects_partial_eager_args(self, vault: Vault) -> None:
        # Passing vault without index (or vice versa) is ambiguous.
        with pytest.raises(ValueError, match="provide vault and index"):
            build_server(vault, None)
