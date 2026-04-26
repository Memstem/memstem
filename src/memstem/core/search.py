"""Hybrid keyword + semantic search via Reciprocal Rank Fusion.

The two signals come from `Index.query_fts` (BM25) and `Index.query_vec`
(L2 distance over chunk embeddings). RRF blends them by inverse rank, which
sidesteps the score-normalization problem inherent to combining BM25 and
distance scores directly.

For each candidate memory, the materialized `Result` carries the per-source
rank so callers can debug ranking decisions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from memstem.core.embeddings import Embedder
from memstem.core.index import FtsHit, Index, VecHit
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)

DEFAULT_RRF_K = 60
OVERFETCH_MULTIPLIER = 5
_FTS_SPECIAL_RE = re.compile(r"[^\w\s]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class FusedHit:
    """A memory id with its blended RRF score and per-source ranks."""

    memory_id: str
    score: float
    bm25_rank: int | None = None
    vec_rank: int | None = None


@dataclass(frozen=True, slots=True)
class Result:
    """A materialized search hit: the canonical Memory + ranking metadata."""

    memory: Memory
    score: float
    bm25_rank: int | None
    vec_rank: int | None


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5-special characters so natural-language queries don't blow up.

    A query like "what's-new?" otherwise raises a syntax error from FTS5.
    Anything left after stripping is matched as an unquoted token list.
    """
    cleaned = _FTS_SPECIAL_RE.sub(" ", query).strip()
    return cleaned


def rrf_combine(
    bm25_hits: list[FtsHit],
    vec_hits: list[VecHit],
    k: int = DEFAULT_RRF_K,
) -> list[FusedHit]:
    """Reciprocal Rank Fusion of BM25 and vector hits.

    Score for each memory = sum(1 / (k + rank)) across both lists. Vector
    hits at the chunk level are de-duplicated to one rank per memory, taking
    the best (first-seen) occurrence.
    """
    fused: dict[str, FusedHit] = {}

    for rank, fts_hit in enumerate(bm25_hits, start=1):
        contribution = 1.0 / (k + rank)
        existing = fused.get(fts_hit.memory_id)
        if existing is None:
            fused[fts_hit.memory_id] = FusedHit(
                memory_id=fts_hit.memory_id,
                score=contribution,
                bm25_rank=rank,
                vec_rank=None,
            )
        else:
            fused[fts_hit.memory_id] = FusedHit(
                memory_id=existing.memory_id,
                score=existing.score + contribution,
                bm25_rank=rank,
                vec_rank=existing.vec_rank,
            )

    seen_for_vec: set[str] = set()
    vec_rank = 0
    for vec_hit in vec_hits:
        if vec_hit.memory_id in seen_for_vec:
            continue
        seen_for_vec.add(vec_hit.memory_id)
        vec_rank += 1
        contribution = 1.0 / (k + vec_rank)
        existing = fused.get(vec_hit.memory_id)
        if existing is None:
            fused[vec_hit.memory_id] = FusedHit(
                memory_id=vec_hit.memory_id,
                score=contribution,
                bm25_rank=None,
                vec_rank=vec_rank,
            )
        else:
            fused[vec_hit.memory_id] = FusedHit(
                memory_id=existing.memory_id,
                score=existing.score + contribution,
                bm25_rank=existing.bm25_rank,
                vec_rank=vec_rank,
            )

    return sorted(fused.values(), key=lambda h: h.score, reverse=True)


class Search:
    """Hybrid search orchestrator over a `Vault` + `Index` (+ optional embedder)."""

    def __init__(
        self,
        vault: Vault,
        index: Index,
        embedder: Embedder | None = None,
    ) -> None:
        self.vault = vault
        self.index = index
        self.embedder = embedder

    def query_bm25(
        self,
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[FtsHit]:
        sanitized = _sanitize_fts_query(query)
        if not sanitized:
            return []
        try:
            return self.index.query_fts(sanitized, limit=limit, types=types)
        except Exception as exc:
            logger.warning("FTS query failed for %r: %s", query, exc)
            return []

    def query_vec(
        self,
        query_embedding: list[float],
        limit: int = 10,
        types: list[str] | None = None,
    ) -> list[VecHit]:
        return self.index.query_vec(query_embedding, limit=limit, types=types)

    def search(
        self,
        query: str,
        limit: int = 10,
        types: list[str] | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> list[Result]:
        """Run hybrid search and materialize the top `limit` results from the vault.

        BM25 always runs. Vector search runs only when an embedder is configured;
        on embedder failure we log and fall back to BM25-only so the daemon
        never goes mute.
        """
        bm25 = self.query_bm25(query, limit=limit * OVERFETCH_MULTIPLIER, types=types)

        vec: list[VecHit] = []
        if self.embedder is not None:
            try:
                query_embedding = self.embedder.embed(query)
                vec = self.query_vec(
                    query_embedding,
                    limit=limit * OVERFETCH_MULTIPLIER,
                    types=types,
                )
            except Exception as exc:
                logger.warning("vec query failed; falling back to BM25: %s", exc)

        fused = rrf_combine(bm25, vec, k=rrf_k)[:limit]
        return self._materialize(fused)

    def _materialize(self, hits: list[FusedHit]) -> list[Result]:
        results: list[Result] = []
        for hit in hits:
            row = self.index.db.execute(
                "SELECT path FROM memories WHERE id = ?",
                (hit.memory_id,),
            ).fetchone()
            if row is None:
                logger.warning("hit %s missing from memories table", hit.memory_id)
                continue
            try:
                memory = self.vault.read(row["path"])
            except MemoryNotFoundError:
                logger.warning(
                    "hit %s references missing vault file %s",
                    hit.memory_id,
                    row["path"],
                )
                continue
            results.append(
                Result(
                    memory=memory,
                    score=hit.score,
                    bm25_rank=hit.bm25_rank,
                    vec_rank=hit.vec_rank,
                )
            )
        return results


__all__ = [
    "DEFAULT_RRF_K",
    "FusedHit",
    "Result",
    "Search",
    "rrf_combine",
]
