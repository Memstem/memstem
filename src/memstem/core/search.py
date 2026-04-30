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
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from memstem.core.embeddings import Embedder
from memstem.core.hyde import HydeExpander, NoOpExpander
from memstem.core.index import FtsHit, Index, VecHit
from memstem.core.mmr import mmr_rerank
from memstem.core.rerank import (
    DEFAULT_RERANK_TOP_N,
    NoOpReranker,
    RerankCandidate,
    Reranker,
)
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
        reranker: Reranker | None = None,
        hyde: HydeExpander | None = None,
    ) -> None:
        self.vault = vault
        self.index = index
        self.embedder = embedder
        # NoOp default keeps the search path branch-free: callers that
        # set ``rerank_top_n`` without configuring a reranker get a
        # silent passthrough (every score 1.0, stable sort preserves
        # the input order). ADR 0017.
        self.reranker = reranker if reranker is not None else NoOpReranker()
        # NoOp default for HyDE: returns the query unchanged so
        # ``use_hyde=True`` without a configured expander is a no-op
        # rather than a crash. ADR 0018.
        self.hyde = hyde if hyde is not None else NoOpExpander()

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
        mmr_lambda: float | None = None,
        rerank_top_n: int | None = None,
        use_hyde: bool = False,
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

        ``mmr_lambda`` enables Maximal Marginal Relevance diversification
        (ADR 0016) on the materialized top-K. ``None`` (the default)
        disables MMR — RRF + importance ordering is final. A float in
        ``[0, 1]`` activates the diversifier: ``0.7`` is the literature
        default; ``1.0`` reduces to identity; ``0.0`` is pure novelty.
        MMR adds one cosine computation per ``(picked, candidate)`` pair
        — bounded by ``limit`` — so the overhead is negligible for
        typical top-10 queries.

        ``rerank_top_n`` enables cross-encoder reranking (ADR 0017).
        ``None`` (the default) skips rerank entirely. An integer N
        re-scores the top-N materialized candidates via
        :attr:`reranker` and re-sorts by the new score before MMR /
        truncation. Rerank ties break on the original RRF score so
        ordering is deterministic. The rerank stage runs *before* MMR
        so MMR diversifies the precision-ordered pool rather than
        the importance-ordered one.

        ``use_hyde`` enables HyDE query expansion (ADR 0018). When
        ``False`` (the default), the original query embedding feeds
        vec retrieval. When ``True``, the configured :attr:`hyde`
        expander rewrites the query into a hypothetical-answer passage
        (subject to its ``should_expand`` gate); that passage is
        embedded as the vec query. BM25 always uses the original
        query — HyDE replaces semantic-space proximity, not lexical
        match. With no embedder configured, HyDE is silently skipped.
        """
        bm25 = self.query_bm25(query, limit=limit * OVERFETCH_MULTIPLIER, types=types)

        vec: list[VecHit] = []
        query_embedding: list[float] | None = None
        if self.embedder is not None:
            embed_input = self._maybe_expand_for_hyde(query, use_hyde=use_hyde)
            try:
                query_embedding = self.embedder.embed(embed_input)
                vec = self.query_vec(
                    query_embedding,
                    limit=limit * OVERFETCH_MULTIPLIER,
                    types=types,
                )
            except Exception as exc:
                logger.warning("vec query failed; falling back to BM25: %s", exc)
                query_embedding = None

        fused = rrf_combine(
            bm25,
            vec,
            k=rrf_k,
            bm25_weight=bm25_weight,
            vector_weight=vector_weight,
        )
        # Materialize a wider pool when MMR or rerank wants more
        # candidates than the final ``limit``. The pool size is the
        # max of all enabled stages so each stage gets the breadth it
        # asked for. ADR 0016 (MMR) defaults to OVERFETCH_MULTIPLIER *
        # limit; ADR 0017 (rerank) sizes by ``rerank_top_n``.
        materialize_limit = limit
        if mmr_lambda is not None and query_embedding is not None:
            materialize_limit = max(materialize_limit, limit * OVERFETCH_MULTIPLIER)
        if rerank_top_n is not None and rerank_top_n > 0:
            materialize_limit = max(materialize_limit, rerank_top_n)
        results = self._materialize(
            fused,
            limit=materialize_limit,
            importance_weight=importance_weight,
            include_expired=include_expired,
            include_deprecated=include_deprecated,
        )
        if rerank_top_n is not None and rerank_top_n > 0 and results:
            results = self._apply_rerank(query, results, top_n=rerank_top_n)
        if mmr_lambda is not None and query_embedding is not None:
            results = mmr_rerank(
                results,
                query_embedding,
                lambda r: self._first_chunk_embedding(str(r.memory.id)),
                lambda_=mmr_lambda,
                k=limit,
            )
        else:
            results = results[:limit]
        if log_client is not None and results:
            self._log_results(
                query=query, results=results, client=log_client, max_rows=log_max_rows
            )
        return results

    def _first_chunk_embedding(self, memory_id: str) -> list[float] | None:
        """Return the first chunk's embedding for ``memory_id``.

        Used by MMR to compute pairwise similarity between candidates.
        Returns ``None`` when the memory has no vectors (e.g. embedder
        failed at ingest or the worker hasn't drained yet) — MMR handles
        this case by appending such candidates after the diversified pool.
        """
        with self.index._lock:
            row = self.index.db.execute(
                """
                SELECT embedding FROM memories_vec
                WHERE memory_id = ? AND chunk_index = 0
                """,
                (memory_id,),
            ).fetchone()
        if row is None:
            return None
        blob = row[0]
        if not isinstance(blob, bytes | bytearray):
            return None
        n_floats = len(blob) // 4
        if n_floats == 0:
            return None
        return list(struct.unpack(f"{n_floats}f", blob))

    def _maybe_expand_for_hyde(self, query: str, *, use_hyde: bool) -> str:
        """Return the string to feed the embedder for vec retrieval.

        Per ADR 0018: HyDE expands the query into a hypothetical
        passage and embeds the passage in place of the query. The
        original query is preserved for BM25.

        Skips expansion when:
        - ``use_hyde`` is False (default) — the bypass path.
        - The expander's ``should_expand`` gate rejects the query.
        - The expander returns the empty string (LLM unreachable);
          falling back keeps search alive.
        """
        if not use_hyde:
            return query
        if not self.hyde.should_expand(query):
            return query
        with self.index._lock:
            hypothesis = self.hyde.expand_cached(query, db=self.index.db)
        if not hypothesis:
            return query
        return hypothesis

    def _apply_rerank(
        self,
        query: str,
        results: list[Result],
        top_n: int,
    ) -> list[Result]:
        """Re-score the top-N materialized results via the configured reranker.

        Per ADR 0017: the rerank stage runs after RRF + importance and
        before MMR. Only the first ``top_n`` results are re-scored;
        anything past ``top_n`` keeps its original RRF order. The
        re-scored slice is sorted by ``(rerank_score, original_score)``
        descending so RRF acts as the tiebreaker.

        On any reranker failure, the candidates retain their input
        order — :meth:`Reranker.score_candidates` already swallows
        per-call exceptions and returns ``0.0`` for that slot, so the
        fallback degrades gracefully rather than crashing the search.
        """
        head = results[:top_n]
        tail = results[top_n:]
        candidates = [RerankCandidate.from_memory(r.memory) for r in head]
        with self.index._lock:
            scores = self.reranker.score_candidates(query, candidates, db=self.index.db)
        # Pair (rerank_score, original RRF score) for stable composite
        # ordering. Higher rerank wins; ties break on RRF (which itself
        # already encodes importance boost) so two ``1.0`` reranks
        # don't shuffle randomly.
        ordered = sorted(
            zip(scores, head, strict=True),
            key=lambda pair: (pair[0], pair[1].score),
            reverse=True,
        )
        return [r for _, r in ordered] + tail

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
    "DEFAULT_RERANK_TOP_N",
    "DEFAULT_RRF_K",
    "FusedHit",
    "Result",
    "Search",
    "rrf_combine",
]
