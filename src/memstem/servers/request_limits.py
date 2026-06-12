"""Request-edge limits shared by the MCP and HTTP servers.

Both servers accept caller-supplied ``limit`` (and, on HTTP,
``rerank_top_n``) values. Callers are agents, and agents occasionally
pass pathological values — a huge ``limit`` turns one search into a
full-index scan plus an oversized MMR pass, and a huge ``rerank_top_n``
fans that many candidate documents out to the shared reranker LLM. On a
multi-tenant box one runaway request degrades every tenant's recall
latency at once, so the values are clamped where they enter, not where
they're consumed.

Clamping (rather than rejecting) matches the upsert path's
normalize-never-reject philosophy: the agent still gets an answer, just
a bounded one. Operator-configured defaults (``SearchConfig``) are NOT
clamped — these bounds apply only to per-request values arriving over
the wire.
"""

from __future__ import annotations

MAX_SEARCH_LIMIT = 100
"""Upper bound on a caller-supplied search ``limit``. Generous for any
real recall flow (the CLI default is 10) while keeping the MMR pass and
result serialization bounded."""

MAX_RERANK_TOP_N = 50
"""Upper bound on a caller-supplied ``rerank_top_n``. Each candidate is
a separate document shipped to the reranker model, so this directly caps
per-request load on the shared LLM backend."""


def clamp_limit(value: int) -> int:
    """Clamp a caller-supplied search limit into ``[1, MAX_SEARCH_LIMIT]``."""
    return max(1, min(value, MAX_SEARCH_LIMIT))


def clamp_rerank_top_n(value: int) -> int:
    """Clamp a caller-supplied rerank_top_n into ``[1, MAX_RERANK_TOP_N]``."""
    return max(1, min(value, MAX_RERANK_TOP_N))


__all__ = [
    "MAX_RERANK_TOP_N",
    "MAX_SEARCH_LIMIT",
    "clamp_limit",
    "clamp_rerank_top_n",
]
