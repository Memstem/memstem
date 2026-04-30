"""Maximal Marginal Relevance (MMR) diversification.

After RRF + importance boost, the top-K of a hybrid search can still
be dominated by paraphrases or near-duplicates of the same fact —
especially when the vault hasn't been through retro dedup yet. MMR
re-ranks the top-K so each pick balances *relevance to the query*
against *redundancy with the picks already made*.

Standard greedy formula::

    mmr_score(c) = λ * sim(c, query) - (1 - λ) * max(sim(c, picked))

with ``λ ∈ [0, 1]``:

- ``λ = 1.0`` → pure relevance, no diversification (RRF order preserved).
- ``λ = 0.7`` → literature default; meaningful but mild diversification.
- ``λ = 0.5`` → balanced; can drop relevant-but-similar results.
- ``λ = 0.0`` → pure novelty; useful only as an upper bound.

This module is pure: no I/O, no SQLite, no LLM. Callers thread an
embedding-lookup callable in so the module can stay decoupled from
the index layer.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import TypeVar

# Default MMR weight. The literature converges on λ ≈ 0.7 for
# user-facing search — diverse enough to break duplicate runs without
# pushing the most-relevant result off the page.
DEFAULT_MMR_LAMBDA = 0.7

# Default number of items to keep after MMR. Caller usually overrides.
DEFAULT_MMR_K = 10


T = TypeVar("T")
"""The candidate type. Module is generic so we don't pay an import
cycle against ``core.search.Result``; callers pass any item type and
an embedding-lookup callable that knows how to fetch its embedding."""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity in ``[-1, 1]``; ``0.0`` for any degenerate input.

    A small utility duplicated here from :mod:`memstem.hygiene.dedup_candidates`
    so the module stays standalone and dependency-light. Both functions
    are byte-identical in behavior; if a third user emerges, hoist them
    to a shared ``core.vector_math`` module.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(ai * bi for ai, bi in zip(a, b, strict=True))
    na = math.sqrt(sum(ai * ai for ai in a))
    nb = math.sqrt(sum(bi * bi for bi in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def mmr_rerank(
    candidates: list[T],
    query_embedding: Sequence[float],
    embedding_lookup: Callable[[T], list[float] | None],
    *,
    lambda_: float = DEFAULT_MMR_LAMBDA,
    k: int = DEFAULT_MMR_K,
) -> list[T]:
    """Re-rank ``candidates`` greedily by MMR; return at most ``k`` items.

    Args:
        candidates: Already-ranked top-N items from RRF + importance.
            Order is the input ranking; MMR may permute it.
        query_embedding: Vector representation of the search query
            (same dim as candidate embeddings).
        embedding_lookup: ``T -> list[float] | None``. Returns the
            candidate's representative embedding (typically its first
            chunk). Returning ``None`` is allowed for candidates without
            vectors — they're appended to the end of the MMR-ordered
            list at original ranking.
        lambda_: MMR weight. ``1.0`` reduces to identity; ``0.0`` to
            pure novelty. Default :data:`DEFAULT_MMR_LAMBDA`.
        k: Max items to return. Defaults to :data:`DEFAULT_MMR_K`.

    Returns:
        At most ``k`` items in MMR order, with no-embedding candidates
        appended at the end (preserving their original relative order).

    Edge cases:

    - Empty ``candidates`` returns an empty list.
    - ``k <= 0`` returns an empty list.
    - Empty ``query_embedding`` returns ``candidates[:k]`` (passthrough).
    - ``lambda_`` is clamped to [0.0, 1.0] silently — out-of-range
      weights typically come from misconfigured YAML.
    """
    if not candidates or k <= 0:
        return []
    if not query_embedding:
        return candidates[:k]

    lambda_clamped = max(0.0, min(1.0, lambda_))

    # Partition candidates into those with embeddings and those without.
    # Indexing by candidate index avoids assumptions about hashability of T.
    embeddings_by_index: dict[int, list[float]] = {}
    with_emb: list[T] = []
    without_emb: list[T] = []
    for i, c in enumerate(candidates):
        emb = embedding_lookup(c)
        if emb is not None and len(emb) == len(query_embedding):
            embeddings_by_index[i] = emb
            with_emb.append(c)
        else:
            without_emb.append(c)

    if not with_emb:
        return candidates[:k]

    # Map T → embedding via positional lookup against the input list.
    # `id()` is a hash-equivalent key that doesn't require T to be
    # hashable (Result and Memory dataclasses aren't, by default).
    embeddings: dict[int, list[float]] = {}
    query_sim: dict[int, float] = {}
    for c in with_emb:
        idx = next(i for i, x in enumerate(candidates) if x is c)
        emb = embeddings_by_index[idx]
        embeddings[id(c)] = emb
        query_sim[id(c)] = cosine_similarity(query_embedding, emb)

    # Greedy MMR selection.
    picked: list[T] = []
    remaining: list[T] = list(with_emb)
    while remaining and len(picked) < k:
        best: T | None = None
        best_score = float("-inf")
        for c in remaining:
            relevance = query_sim[id(c)]
            if not picked:
                redundancy = 0.0
            else:
                redundancy = max(
                    cosine_similarity(embeddings[id(c)], embeddings[id(p)]) for p in picked
                )
            mmr_score = lambda_clamped * relevance - (1.0 - lambda_clamped) * redundancy
            if mmr_score > best_score:
                best_score = mmr_score
                best = c
        # `best` cannot be None here: `remaining` is non-empty and the
        # comparison initializes with -inf so the first iteration picks.
        assert best is not None
        picked.append(best)
        remaining.remove(best)

    # No-embedding candidates fill the remaining slots in original order.
    return (picked + without_emb)[:k]


__all__ = [
    "DEFAULT_MMR_K",
    "DEFAULT_MMR_LAMBDA",
    "cosine_similarity",
    "mmr_rerank",
]
