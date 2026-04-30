"""Tests for ``memstem.core.mmr`` — Maximal Marginal Relevance."""

from __future__ import annotations

from pathlib import Path

import pytest

from memstem.core.frontmatter import Frontmatter, validate
from memstem.core.mmr import (
    DEFAULT_MMR_K,
    DEFAULT_MMR_LAMBDA,
    cosine_similarity,
    mmr_rerank,
)
from memstem.core.search import Result
from memstem.core.storage import Memory


def _result(*, memory_id: str, score: float = 0.5, title: str = "untitled") -> Result:
    fm: Frontmatter = validate(
        {
            "id": memory_id,
            "type": "memory",
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z",
            "source": "test",
            "title": title,
        }
    )
    memory = Memory(frontmatter=fm, body="body", path=Path(f"memories/{memory_id}.md"))
    return Result(memory=memory, score=score, bm25_rank=1, vec_rank=None)


# ─── cosine_similarity ──────────────────────────────────────────────


def test_cosine_identical_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_degenerate_inputs_zero() -> None:
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


# ─── mmr_rerank edge cases ──────────────────────────────────────────


def test_empty_candidates_returns_empty() -> None:
    assert mmr_rerank([], [1.0, 0.0], lambda _r: None) == []


def test_zero_k_returns_empty() -> None:
    candidates = [_result(memory_id="11111111-1111-1111-1111-111111111111")]
    assert mmr_rerank(candidates, [1.0, 0.0], lambda _r: [1.0, 0.0], k=0) == []


def test_empty_query_embedding_passthrough() -> None:
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    out = mmr_rerank([a, b], [], lambda _r: [1.0, 0.0])
    assert out == [a, b]


def test_no_embeddings_passthrough() -> None:
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    out = mmr_rerank([a, b], [1.0, 0.0], lambda _r: None)
    assert out == [a, b]


# ─── mmr_rerank behavior ────────────────────────────────────────────


def test_lambda_one_preserves_relevance_order() -> None:
    """λ=1 reduces MMR to pure relevance: highest sim(query, c) wins."""
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    c = _result(memory_id="33333333-3333-3333-3333-333333333333")
    embeddings = {
        str(a.memory.id): [1.0, 0.0, 0.0],  # sim with [1,0,0] = 1.0
        str(b.memory.id): [0.5, 0.5, 0.0],  # sim ~ 0.707
        str(c.memory.id): [0.0, 1.0, 0.0],  # sim = 0.0
    }
    out = mmr_rerank(
        [a, b, c],
        [1.0, 0.0, 0.0],
        lambda r: embeddings.get(str(r.memory.id)),
        lambda_=1.0,
    )
    assert [str(r.memory.id) for r in out] == [
        str(a.memory.id),
        str(b.memory.id),
        str(c.memory.id),
    ]


def test_lambda_zero_promotes_diversity() -> None:
    """λ=0 reduces MMR to pure novelty: after the first pick, the most-distinct wins."""
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b_dup = _result(memory_id="22222222-2222-2222-2222-222222222222")
    c_distinct = _result(memory_id="33333333-3333-3333-3333-333333333333")
    embeddings = {
        str(a.memory.id): [1.0, 0.0, 0.0],
        # Near-identical to a → high similarity to picked.
        str(b_dup.memory.id): [0.99, 0.0, 0.0],
        # Orthogonal to a → maximally novel.
        str(c_distinct.memory.id): [0.0, 1.0, 0.0],
    }
    out = mmr_rerank(
        [a, b_dup, c_distinct],
        [1.0, 0.0, 0.0],
        lambda r: embeddings.get(str(r.memory.id)),
        lambda_=0.0,
        k=3,
    )
    # First pick: tie at 0.0 redundancy, MMR picks the first encountered.
    # We assert second pick is `c_distinct`, not `b_dup`.
    second_id = str(out[1].memory.id)
    assert second_id == str(c_distinct.memory.id)


def test_default_lambda_breaks_duplicate_streak() -> None:
    """The default λ=0.7 demotes a paraphrase when distinct alternative is equally relevant.

    Geometry note: when query and the first-picked record point in
    the same direction, MMR cannot distinguish a paraphrase from a
    distinct-but-relevant record because both have rel == red. The
    diversification effect is observable when the query has support
    in two directions and an alternative covers the "second" direction.
    """
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    a_paraphrase = _result(memory_id="22222222-2222-2222-2222-222222222222")
    distinct = _result(memory_id="33333333-3333-3333-3333-333333333333")
    # Query has support in both x and z. `a` covers x; `distinct`
    # covers z. Paraphrase nearly duplicates a — same x coverage,
    # nothing on z.
    query = [1.0, 0.0, 1.0]
    embeddings = {
        str(a.memory.id): [1.0, 0.0, 0.0],
        str(a_paraphrase.memory.id): [0.99, 0.01, 0.0],
        str(distinct.memory.id): [0.0, 0.0, 1.0],
    }
    out = mmr_rerank(
        [a, a_paraphrase, distinct],
        query,
        lambda r: embeddings.get(str(r.memory.id)),
        lambda_=DEFAULT_MMR_LAMBDA,
        k=2,
    )
    assert str(out[0].memory.id) == str(a.memory.id)
    # Second pick should be the distinct one — diversification wins
    # because `distinct` is orthogonal to picked `a`, so its redundancy
    # contribution is 0 vs. ~1.0 for the paraphrase.
    assert str(out[1].memory.id) == str(distinct.memory.id)


def test_no_embedding_candidates_appended_at_end() -> None:
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    no_emb = _result(memory_id="33333333-3333-3333-3333-333333333333")

    embeddings = {
        str(a.memory.id): [1.0, 0.0],
        str(b.memory.id): [0.0, 1.0],
    }

    def lookup(r: Result) -> list[float] | None:
        return embeddings.get(str(r.memory.id))

    out = mmr_rerank([a, b, no_emb], [1.0, 0.0], lookup, k=3)
    assert len(out) == 3
    # The two with embeddings come first (in MMR-decided order); no_emb last.
    assert str(out[2].memory.id) == str(no_emb.memory.id)


def test_k_truncates_results() -> None:
    """``k`` caps how many results come back."""
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    c = _result(memory_id="33333333-3333-3333-3333-333333333333")
    embeddings = {
        str(a.memory.id): [1.0, 0.0, 0.0],
        str(b.memory.id): [0.5, 0.5, 0.0],
        str(c.memory.id): [0.0, 1.0, 0.0],
    }
    out = mmr_rerank(
        [a, b, c],
        [1.0, 0.0, 0.0],
        lambda r: embeddings.get(str(r.memory.id)),
        k=2,
    )
    assert len(out) == 2


def test_lambda_clamped_silently() -> None:
    """λ outside [0, 1] is clamped rather than raising."""
    a = _result(memory_id="11111111-1111-1111-1111-111111111111")
    b = _result(memory_id="22222222-2222-2222-2222-222222222222")
    embeddings = {
        str(a.memory.id): [1.0, 0.0],
        str(b.memory.id): [0.0, 1.0],
    }
    # λ=2.0 → clamped to 1.0 → pure relevance.
    out = mmr_rerank(
        [a, b],
        [1.0, 0.0],
        lambda r: embeddings.get(str(r.memory.id)),
        lambda_=2.0,
    )
    assert str(out[0].memory.id) == str(a.memory.id)
    # λ=-1.0 → clamped to 0.0 → pure novelty (after first pick).
    out = mmr_rerank(
        [a, b],
        [1.0, 0.0],
        lambda r: embeddings.get(str(r.memory.id)),
        lambda_=-1.0,
        k=2,
    )
    assert len(out) == 2


def test_default_constants() -> None:
    """Sanity-check the public defaults haven't drifted."""
    assert 0.0 < DEFAULT_MMR_LAMBDA < 1.0
    assert DEFAULT_MMR_K >= 1
