"""Config validation: nonsense numeric values must fail at load time.

Pre-fix, `workers: 0` crashed the daemon at startup with a bare
ValueError from run_workers, `batch_size: 0` silently stalled embedding
forever (claim_pending(limit=0) returns nothing), and negative
intervals produced busy loops. Pydantic field constraints now reject
all of these when config.yaml is parsed, with a message naming the
offending field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from memstem.config import Config, EmbeddingConfig, HygieneConfig
from memstem.core.embed_worker import EmbedWorker


def _config(**overrides: object) -> Config:
    return Config.model_validate({"vault_path": "/tmp/vault", **overrides})


class TestEmbeddingValidation:
    def test_workers_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="workers"):
            _config(embedding={"workers": 0})

    def test_workers_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="workers"):
            _config(embedding={"workers": -2})

    def test_batch_size_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="batch_size"):
            _config(embedding={"batch_size": 0})

    def test_dimensions_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dimensions"):
            _config(embedding={"dimensions": 0})

    def test_defaults_valid(self) -> None:
        cfg = _config()
        assert cfg.embedding.workers == 2
        assert cfg.embedding.batch_size == 8


class TestIntervalValidation:
    def test_negative_hygiene_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="distill_interval_seconds"):
            _config(hygiene={"distill_interval_seconds": -1})

    def test_zero_poll_interval_rejected(self) -> None:
        # 0 here would busy-loop the hygiene scheduler.
        with pytest.raises(ValidationError, match="loop_poll_interval_seconds"):
            _config(hygiene={"loop_poll_interval_seconds": 0})

    def test_negative_reconcile_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reconcile_interval_seconds"):
            _config(adapters={"reconcile_interval_seconds": -3600})

    def test_zero_means_disabled_still_allowed(self) -> None:
        # 0 is the documented "disabled" value for these knobs and must
        # keep working.
        cfg = _config(
            adapters={"reconcile_interval_seconds": 0},
            mcp={"idle_timeout_seconds": 0},
            hygiene={"distill_interval_seconds": 0},
        )
        assert cfg.adapters.reconcile_interval_seconds == 0
        assert cfg.mcp.idle_timeout_seconds == 0
        assert cfg.hygiene.distill_interval_seconds == 0

    def test_negative_idle_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError, match="idle_timeout_seconds"):
            _config(mcp={"idle_timeout_seconds": -1})


class TestServerValidation:
    def test_port_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="port"):
            _config(http={"port": 0})

    def test_port_above_range_rejected(self) -> None:
        with pytest.raises(ValidationError, match="port"):
            _config(http={"port": 65536})


class TestSubmodelsDirect:
    """The submodels validate standalone too (for_provider, tests, API users)."""

    def test_embedding_config_direct(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingConfig(workers=0)

    def test_hygiene_config_direct(self) -> None:
        with pytest.raises(ValidationError):
            HygieneConfig(importance_interval_seconds=-5)


class TestWorkerGuard:
    """`memstem embed --batch-size N` bypasses config.yaml — the worker
    itself must reject a paralyzing batch size."""

    def test_batch_size_zero_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            EmbedWorker(vault=None, index=None, embedder=None, batch_size=0)  # type: ignore[arg-type]

    def test_batch_size_negative_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            EmbedWorker(vault=None, index=None, embedder=None, batch_size=-5)  # type: ignore[arg-type]
