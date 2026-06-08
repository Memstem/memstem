"""Config defaults for the search-time reranker + MMR knobs (ADR 0016/0017)."""

from __future__ import annotations

from memstem.config import RerankerConfig, SearchConfig


class TestSearchConfigDefaults:
    def test_rerank_and_mmr_off_by_default(self) -> None:
        sc = SearchConfig()
        # Backwards-compatible: nothing changes for existing vaults.
        assert sc.mmr_lambda is None
        assert sc.rerank_top_n is None
        assert isinstance(sc.reranker, RerankerConfig)
        assert sc.reranker.enabled is False

    def test_reranker_config_defaults(self) -> None:
        rc = RerankerConfig()
        assert rc.enabled is False
        assert rc.provider == "openai"
        assert rc.model == "gpt-4o-mini"
        assert rc.base_url is None
        assert rc.api_key_env == "OPENAI_API_KEY"

    def test_self_hosted_gemma_reranker_roundtrips(self) -> None:
        # The fleet's validated recipe: Gemma box as an OpenAI-compatible reranker.
        sc = SearchConfig(
            mmr_lambda=0.5,
            rerank_top_n=15,
            reranker=RerankerConfig(
                enabled=True,
                provider="openai",
                model="gemma-4-e4b-it",
                base_url="http://localhost:8000/v1",
            ),
        )
        assert sc.mmr_lambda == 0.5
        assert sc.rerank_top_n == 15
        assert sc.reranker.enabled is True
        assert sc.reranker.model == "gemma-4-e4b-it"
        # model_dump round-trips cleanly for YAML persistence.
        dumped = sc.model_dump()
        assert dumped["reranker"]["base_url"] == "http://localhost:8000/v1"
        assert SearchConfig(**dumped).reranker.model == "gemma-4-e4b-it"
