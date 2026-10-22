"""Tests for query-time recency weighting (ADR 0054)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from memstem.config import RecencyConfig, SearchConfig
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.recency import (
    RecencyPolicy,
    content_date,
    is_historical_query,
    policy_for_query,
    policy_from_config,
    recency_multiplier,
)
from memstem.core.search import Search
from memstem.core.storage import Memory, Vault

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FOLDERS = {
    "memory": "memories",
    "session": "sessions",
    "skill": "skills",
    "distillation": "distillations",
    "decision": "decisions",
}


def _memory(
    *,
    type_: str = "memory",
    updated: datetime = NOW,
    body: str = "alpha topic",
    extra: dict[str, Any] | None = None,
) -> Memory:
    metadata: dict[str, Any] = {
        "id": str(uuid4()),
        "type": type_,
        "created": updated.isoformat(),
        "updated": updated.isoformat(),
        "source": "test",
        "title": f"item-{type_}",
        "tags": [],
    }
    if type_ == "skill":
        metadata["scope"] = "universal"
        metadata["verification"] = "verify by hand"
    metadata.update(extra or {})
    fm = validate(metadata)
    return Memory(frontmatter=fm, body=body, path=Path(f"{FOLDERS[type_]}/{fm.id}.md"))


class TestConfig:
    def test_off_by_default(self) -> None:
        assert SearchConfig().recency.enabled is False
        assert policy_from_config(SearchConfig().recency) is None

    def test_enabled_config_builds_policy(self) -> None:
        cfg = RecencyConfig(enabled=True, weight=0.2, half_life_days=14, timeless_types=["skill"])
        policy = policy_from_config(cfg)
        assert policy == RecencyPolicy(
            weight=0.2, half_life_days=14, timeless_types=frozenset({"skill"})
        )

    def test_bounds(self) -> None:
        with pytest.raises(ValueError):
            RecencyConfig(weight=1.5)
        with pytest.raises(ValueError):
            RecencyConfig(half_life_days=0)


class TestMultiplier:
    policy = RecencyPolicy(weight=0.1, half_life_days=30)

    def test_fresh_gets_full_boost(self) -> None:
        assert recency_multiplier(_memory(), NOW, self.policy) == pytest.approx(1.1)

    def test_halves_every_half_life(self) -> None:
        m = _memory(updated=NOW - timedelta(days=30))
        assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1.05)
        m = _memory(updated=NOW - timedelta(days=60))
        assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1.025)

    def test_future_date_clamped(self) -> None:
        m = _memory(updated=NOW + timedelta(days=5))
        assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1.1)

    def test_timeless_types_never_stale(self) -> None:
        old = NOW - timedelta(days=400)
        for type_ in ("skill", "decision"):
            m = _memory(type_=type_, updated=old)
            assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1.1)

    def test_pinned_never_stale(self) -> None:
        m = _memory(updated=NOW - timedelta(days=400), extra={"pinned": True})
        assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1.1)

    def test_distillation_uses_source_session_date(self) -> None:
        source = NOW - timedelta(days=120)
        m = _memory(
            type_="distillation",
            updated=NOW,
            extra={
                "provenance": {
                    "source": "hygiene-worker",
                    "source_updated": source.isoformat(),
                }
            },
        )
        assert content_date(m) == source
        assert recency_multiplier(m, NOW, self.policy) == pytest.approx(1 + 0.1 * 2**-4)

    def test_bad_source_updated_falls_back(self) -> None:
        m = _memory(extra={"provenance": {"source": "x", "source_updated": "not a date"}})
        assert content_date(m) == NOW


class TestHistoricalQueries:
    @pytest.mark.parametrize(
        "query",
        [
            "what did we decide back in June about the reranker",
            "memstem config in 2025",
            "what was the embed provider on 2026-08-01",
            "what did we originally use for embeddings",
            "how did the Oaks NVR work three months ago",
            "when did we switch to OpenRouter",
            "the history of pole 35",
            "early may pricing",
        ],
    )
    def test_historical(self, query: str) -> None:
        assert is_historical_query(query)

    @pytest.mark.parametrize(
        "query",
        [
            "which embedding provider does memstem use",
            "may I restart the daemon",
            "what model does the ticket agent use",
            "current TechPro ticket setup",
            "marching orders for the content engine",
        ],
    )
    def test_not_historical(self, query: str) -> None:
        assert not is_historical_query(query)

    def test_policy_dropped_for_historical(self) -> None:
        policy = RecencyPolicy()
        assert policy_for_query(policy, "what did we use back in June") is None
        assert policy_for_query(policy, "what do we use now") is policy

    def test_skip_can_be_disabled(self) -> None:
        policy = RecencyPolicy(skip_historical_queries=False)
        assert policy_for_query(policy, "what did we use back in June") is policy

    def test_zero_weight_is_off(self) -> None:
        assert policy_for_query(RecencyPolicy(weight=0.0), "anything") is None


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in (*FOLDERS.values(), "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


def _store(vault: Vault, index: Index, memory: Memory) -> Memory:
    vault.write(memory)
    index.upsert(memory)
    return memory


class TestSearchIntegration:
    """The older record has the stronger keyword match, so it leads on RRF;
    recency should flip the order and scale each score by its multiplier."""

    def _pair(self, vault: Vault, index: Index, words: str = "alpha") -> tuple[Memory, Memory]:
        now = datetime.now(tz=UTC)
        old = _store(
            vault, index, _memory(updated=now - timedelta(days=200), body=f"{words} {words} topic")
        )
        new = _store(vault, index, _memory(updated=now - timedelta(days=1), body=f"{words} topic"))
        return old, new

    def _scores(self, vault: Vault, index: Index, query: str, **kw: Any) -> dict[str, float]:
        results = Search(vault=vault, index=index).search(
            query, limit=2, importance_weight=0.0, log_client=None, **kw
        )
        return {str(r.memory.id): r.score for r in results}

    def test_newer_ranks_first_when_enabled(self, vault: Vault, index: Index) -> None:
        old, new = self._pair(vault, index)
        results = Search(vault=vault, index=index).search(
            "alpha", limit=2, importance_weight=0.0, log_client=None
        )
        assert [r.memory.id for r in results] == [old.id, new.id]
        results = Search(vault=vault, index=index).search(
            "alpha", limit=2, importance_weight=0.0, log_client=None, recency=RecencyPolicy()
        )
        assert [r.memory.id for r in results] == [new.id, old.id]

    def test_scores_scaled_by_multiplier(self, vault: Vault, index: Index) -> None:
        old, new = self._pair(vault, index)
        base = self._scores(vault, index, "alpha")
        boosted = self._scores(vault, index, "alpha", recency=RecencyPolicy())
        assert boosted[str(new.id)] / base[str(new.id)] == pytest.approx(
            1 + 0.1 * 2 ** (-1 / 30), rel=1e-3
        )
        assert boosted[str(old.id)] / base[str(old.id)] == pytest.approx(
            1 + 0.1 * 2 ** (-200 / 30), rel=1e-3
        )

    def test_historical_query_gets_no_edge(self, vault: Vault, index: Index) -> None:
        self._pair(vault, index, words="alpha originally")
        query = "alpha originally"
        assert is_historical_query(query)
        base = self._scores(vault, index, query)
        assert len(base) == 2
        assert self._scores(vault, index, query, recency=RecencyPolicy()) == base

    def test_old_skill_not_outranked_by_new_memory(self, vault: Vault, index: Index) -> None:
        now = datetime.now(tz=UTC)
        skill = _store(vault, index, _memory(type_="skill", updated=now - timedelta(days=300)))
        new = _store(vault, index, _memory(updated=now - timedelta(days=1)))
        scores = self._scores(vault, index, "alpha", recency=RecencyPolicy())
        base = self._scores(vault, index, "alpha")
        assert scores[str(skill.id)] / base[str(skill.id)] == pytest.approx(1.1)
        assert scores[str(new.id)] / base[str(new.id)] < 1.1
