"""Tests for the shared request-edge clamps."""

from __future__ import annotations

from memstem.servers.request_limits import (
    MAX_RERANK_TOP_N,
    MAX_SEARCH_LIMIT,
    clamp_limit,
    clamp_rerank_top_n,
)


class TestClampLimit:
    def test_normal_values_pass_through(self) -> None:
        assert clamp_limit(10) == 10
        assert clamp_limit(1) == 1
        assert clamp_limit(MAX_SEARCH_LIMIT) == MAX_SEARCH_LIMIT

    def test_oversized_clamped_to_max(self) -> None:
        assert clamp_limit(MAX_SEARCH_LIMIT + 1) == MAX_SEARCH_LIMIT
        assert clamp_limit(10**12) == MAX_SEARCH_LIMIT

    def test_zero_and_negative_floored_to_one(self) -> None:
        assert clamp_limit(0) == 1
        assert clamp_limit(-99) == 1


class TestClampRerankTopN:
    def test_normal_values_pass_through(self) -> None:
        assert clamp_rerank_top_n(5) == 5
        assert clamp_rerank_top_n(MAX_RERANK_TOP_N) == MAX_RERANK_TOP_N

    def test_oversized_clamped_to_max(self) -> None:
        assert clamp_rerank_top_n(MAX_RERANK_TOP_N + 1) == MAX_RERANK_TOP_N

    def test_zero_and_negative_floored_to_one(self) -> None:
        assert clamp_rerank_top_n(0) == 1
        assert clamp_rerank_top_n(-1) == 1
