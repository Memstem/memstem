"""Smoke tests: package installs and basic imports work."""

from __future__ import annotations

from pathlib import Path

import memstem
from memstem.adapters.base import Adapter, MemoryRecord
from memstem.config import Config


def test_package_version() -> None:
    assert memstem.__version__


def test_memory_record_minimal() -> None:
    record = MemoryRecord(source="test", ref="abc", body="hello")
    assert record.source == "test"
    assert record.ref == "abc"
    assert record.body == "hello"
    assert record.title is None
    assert record.tags == []
    assert record.metadata == {}


def test_adapter_is_abstract() -> None:
    assert Adapter.__abstractmethods__ == frozenset({"watch", "reconcile"})


def test_config_defaults() -> None:
    config = Config(vault_path=Path("/tmp/vault"))
    assert config.embedding.provider == "ollama"
    assert config.embedding.model == "nomic-embed-text"
    assert config.search.rrf_k == 60
    assert config.hygiene.dedup_threshold == 0.95
