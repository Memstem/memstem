"""Hybrid keyword + semantic search via Reciprocal Rank Fusion.

The two signals come from `Index.query_fts` (BM25) and `Index.query_vec`
(L2 distance over chunk embeddings). RRF blends them by inverse rank, which
sidesteps the score-normalization problem inherent to combining BM25 and
distance scores directly.

For each candidate memory, the materialized `Result` carries the per-source
rank so callers can debug ranking decisions.

The final score is optionally boosted by ``importance`` (ADR 0008 Tier 1):
``final = rrf * (1 + alpha * importance)`` where ``alpha`` is the
``importance_weight`` config knob. With ``alpha = 0.0`` the RRF order is
preserved exactly (v0.1 behavior); with the default ``alpha = 0.2`` an
importance of ``1.0`` raises a record's score by 20% — enough to break
close ties without overwhelming relevance.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from memstem.core.embeddings import Embedder
from memstem.core.index import FtsHit, Index, VecHit
from memstem.core.retrieval_log import (
    DEFAULT_MAX_ROWS as DEFAULT_QUERY_LOG_MAX_ROWS,
)
from memstem.core.retrieval_log import (
    LoggedHit,
    log_search_results,
)
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)

DEFAULT_RRF_K = 60
DEFAULT_IMPORTANCE_WEIGHT = 0.2
"""ADR 0008 alpha. ``final = rrf * (1 + alpha * importance)``."""
DEFAULT_IMPORTANCE = 0.5
"""Per ADR 0008: a record without an explicit ``importance`` is treated as
neutral (0.5) rather than 0.0. This keeps un-scored records on a level
playing field with explicitly-mid-importance records, and prevents the
boost from making un-scored records *worse* than they would have been
without a boost at all."""
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
    bm25_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[FusedHit]:
    """Reciprocal Rank Fusion of BM25 and vector hits.

    Score for each memory = sum(weight / (k + rank)) across both lists.
    Vector hits at the chunk level are de-duplicated to one rank per memory,
    taking the best (first-seen) occurrence.

    The weights let callers bias toward one signal — set ``bm25_weight=0``
    for vec-only fusion, ``vector_weight=0`` for BM25-only — without having
    to short-circuit the call.
    """
    fused: dict[str, FusedHit] = {}

    for rank, fts_hit in enumerate(bm25_hits, start=1):
        contribution = bm25_weight / (k + rank)
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
        contribution = vector_weight / (k + vec_rank)
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
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0,
        importance_weight: float = DEFAULT_IMPORTANCE_WEIGHT,
        include_expired: bool = False,
        include_deprecated: bool = False,
        log_client: str | None = None,
        log_max_rows: int = DEFAULT_QUERY_LOG_MAX_ROWS,
    ) -> list[Result]:
        """Run hybrid search and materialize the top `limit` results from the vault.

        BM25 always runs. Vector search runs only when an embedder is configured;
        on embedder failure we log and fall back to BM25-only so the daemon
        never goes mute.

        ``rrf_k``, ``bm25_weight``, and ``vector_weight`` control the fusion —
        the CLI and MCP server thread the configured ``SearchConfig`` values
        through here so users can tune ranking from ``_meta/config.yaml``.

        ``importance_weight`` (ADR 0008 Tier 1 alpha) post-multiplies each
        hit's RRF score by ``(1 + alpha * importance)``. ``0.0`` disables
        the boost entirely (RRF order is final). The default ``0.2`` lets
        importance act as a tiebreaker without overwhelming relevance.
        Records without an explicit ``importance`` get the neutral default
        (``0.5``) so un-scored records don't lose ranking just because no
        one annotated them.

        ``log_client`` (ADR 0008 Tier 1 query log) tags retrieval-log rows
        with the call site (``"cli"``, ``"mcp"``, ``"http"``). Pass
        ``None`` to disable logging for this call — useful for tests and
        for internal calls where you don't want to credit anything to
        importance. Logging failures are warned and swallowed; they
        never break search.

        Records whose ``valid_to`` has elapsed are filtered out by default
        (ADR 0011 PR-B). Pass ``include_expired=True`` to surface them
        anyway — useful for audit or debugging "where did that transient
        record go?" questions.

        Records carrying a ``deprecated_by`` pointer (ADR 0012 — the W3
        retro pass and future Layer 3 resolutions both set this) are
        also filtered by default. Pass ``include_deprecated=True`` to
        surface them.
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

        fused = rrf_combine(
            bm25,
            vec,
            k=rrf_k,
            bm25_weight=bm25_weight,
            vector_weight=vector_weight,
        )
        results = self._materialize(
            fused,
            limit=limit,
            importance_weight=importance_weight,
            include_expired=include_expired,
            include_deprecated=include_deprecated,
        )
        if log_client is not None and results:
            self._log_results(
                query=query, results=results, client=log_client, max_rows=log_max_rows
            )
        return results

    def _log_results(
        self,
        *,
        query: str,
        results: list[Result],
        client: str,
        max_rows: int,
    ) -> None:
        """Append one row per surfaced result to the query log.

        Wrapped here (rather than at every call site) so failures stay
        local to the search path's catch — :func:`log_search_results`
        already absorbs ``sqlite3.Error``, but a programming error inside
        this wrapper would otherwise propagate.
        """
        try:
            hits = [
                LoggedHit(memory_id=str(r.memory.id), rank=i + 1, score=r.score)
                for i, r in enumerate(results)
            ]
            log_search_results(
                self.index.db,
                query=query,
                hits=hits,
                client=client,
                max_rows=max_rows,
            )
        except Exception as exc:
            logger.warning("query_log: unexpected error during search logging: %s", exc)

    def _materialize(
        self,
        hits: list[FusedHit],
        limit: int,
        importance_weight: float = DEFAULT_IMPORTANCE_WEIGHT,
        include_expired: bool = False,
        include_deprecated: bool = False,
    ) -> list[Result]:
        """Read each fused hit's `Memory` from the vault, optionally re-score, sort, truncate.

        When ``importance_weight == 0`` the loop short-circuits as soon as
        ``limit`` results accumulate — RRF order is final and we don't
        need to materialize anything further. When the importance boost
        is active (the default), the boost can re-order results, so we
        materialize a wider pool (capped at ``limit * OVERFETCH_MULTIPLIER``)
        before sorting by the boosted score. The materialization cost is
        bounded; the cost is one extra Memory.read() per pool entry that
        wouldn't have been read in the short-circuit path.
        """
        now = datetime.now(tz=UTC)
        boost_active = importance_weight != 0.0
        pool_target = max(limit * OVERFETCH_MULTIPLIER, limit) if boost_active else limit

        pool: list[Result] = []
        for hit in hits:
            if len(pool) >= pool_target:
                break
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
            if not include_expired and self._is_expired(memory, now):
                continue
            if not include_deprecated and memory.frontmatter.deprecated_by is not None:
                continue
            score = self._apply_importance(hit.score, memory, importance_weight)
            pool.append(
                Result(
                    memory=memory,
                    score=score,
                    bm25_rank=hit.bm25_rank,
                    vec_rank=hit.vec_rank,
                )
            )

        if boost_active:
            pool.sort(key=lambda r: r.score, reverse=True)
        return pool[:limit]

    @staticmethod
    def _apply_importance(
        rrf_score: float,
        memory: Memory,
        importance_weight: float,
    ) -> float:
        """Apply ADR 0008's importance multiplier to an RRF score.

        Returns ``rrf_score`` unchanged when ``importance_weight == 0``.
        Otherwise: ``rrf_score * (1 + importance_weight * effective_importance)``,
        where ``effective_importance`` is the frontmatter value or
        :data:`DEFAULT_IMPORTANCE` if unset.
        """
        if importance_weight == 0.0:
            return rrf_score
        importance = memory.frontmatter.importance
        if importance is None:
            importance = DEFAULT_IMPORTANCE
        return rrf_score * (1.0 + importance_weight * importance)

    @staticmethod
    def _is_expired(memory: Memory, now: datetime) -> bool:
        valid_to = memory.frontmatter.valid_to
        return valid_to is not None and valid_to <= now


__all__ = [
    "DEFAULT_IMPORTANCE",
    "DEFAULT_IMPORTANCE_WEIGHT",
    "DEFAULT_RRF_K",
    "FusedHit",
    "Result",
    "Search",
    "rrf_combine",
]
