"""Near-duplicate candidate generator (ADR 0012 Layer 2 first slice).

This module is the Layer 2 piece of ADR 0012's three-layer dedup
pipeline (the existing :mod:`memstem.core.dedup` module is Layer 1's
exact-body hash; the LLM-as-judge is Layer 3, deferred). It walks the
vault's vector index, scans for memory pairs whose first-chunk
embeddings are close in cosine space, and reports them as candidate
duplicates.

**Design constraints (from the user's stage 5 brief):**

- Use vector similarity to find candidate duplicates.
- Add an audit / report command and dry-run output.
- Do **not** automatically delete or merge records.
- If marking candidates, use explicit audit / provenance.
- No destructive behavior by default.

This slice ships the audit report only. The LLM-as-judge that turns a
candidate pair into a definitive ``DUPLICATE`` / ``CONTRADICTS`` /
``RELATED_BUT_DISTINCT`` / ``UNRELATED`` verdict is Stage 6 / a future
PR. Until that lands, the operator reads the report and decides
manually.

**Implementation notes:**

- **First-chunk proxy.** Each memory may be split across multiple
  chunks (the embedder chunks long bodies). For the candidate
  generator, the first chunk is a sufficient proxy — we only need
  *probable* near-duplicates, not exhaustive coverage. Layer 3 (the
  LLM judge) sees full bodies and resolves edge cases.
- **L2 → cosine on unit-norm embeddings.** ``sqlite-vec`` returns L2
  distance. For unit-norm vectors, ``cosine = 1 - L2² / 2``. Most
  embedders Memstem ships (Ollama ``nomic-embed-text``, OpenAI
  ``text-embedding-3-large``, Voyage ``voyage-3``, Gemini
  ``gemini-embedding-2-preview``) produce unit-norm or near-unit-norm
  vectors. For cross-provider safety, the planner re-computes a
  *true* cosine from the raw vectors after vec_search returns
  candidates — the L2 proxy is only used to bound the candidate pool.
- **Pair canonicalization.** Pairs are canonicalized so a→b and b→a
  produce one entry (``a < b`` lexicographically); the same pair
  cannot appear twice.
- **No mutation.** This module does not write to the vault, the
  index, or the hygiene state. It's read-only by design.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass

from memstem.core.index import Index
from memstem.core.storage import MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)


DEFAULT_MIN_COSINE = 0.85
"""ADR 0012 Layer 2 cosine threshold. Deliberately permissive — Layer
3 (the LLM judge) filters false positives. False negatives at this
layer are the failure mode to avoid (a missed candidate never reaches
the judge), so we err on the side of more candidates."""

DEFAULT_NEIGHBORS_PER_MEMORY = 5
"""How many vec-nearest-neighbor candidates to consider per memory. The
ADR specifies 5; tuning this up doesn't help much because anything
beyond rank-5 is rarely a true near-duplicate."""


@dataclass(frozen=True)
class DedupCandidatePair:
    """One audit-worthy near-duplicate pair.

    The pair is *canonical*: ``a_id < b_id`` lexicographically. Both
    sides include path/title metadata so the CLI can render a useful
    report without a second vault round-trip.
    """

    a_id: str
    b_id: str
    cosine: float
    """True cosine similarity computed from the raw chunk embeddings.
    Range ``[-1.0, 1.0]``; ``1.0`` is identical, ``0.0`` is orthogonal.
    The ``min_cosine`` filter operates on this value, not on the L2
    proxy used internally to bound the candidate pool."""

    a_title: str | None
    b_title: str | None
    a_path: str
    b_path: str
    a_type: str
    b_type: str

    @property
    def involves_skill(self) -> bool:
        """True if either side is a skill record.

        ADR 0012 routes skill-vs-anything candidates through a human
        review queue rather than auto-merging them. The audit report
        flags this so the operator knows which pairs are
        skill-sensitive even at the candidate stage.
        """
        return self.a_type == "skill" or self.b_type == "skill"


def _read_chunk_embedding(index: Index, memory_id: str, chunk_index: int = 0) -> list[float] | None:
    """Read one chunk's embedding bytes back from ``memories_vec``.

    Returns ``None`` if the chunk is missing — the caller should treat
    that as "this memory wasn't embedded yet" and skip it.
    """
    with index._lock:
        row = index.db.execute(
            """
            SELECT embedding FROM memories_vec
            WHERE memory_id = ? AND chunk_index = ?
            """,
            (memory_id, chunk_index),
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


def _cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity of two equal-length vectors.

    Returns ``0.0`` for any degenerate input (length mismatch, zero
    vector). True cosine: ``dot(a, b) / (|a| * |b|)``.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(ai * bi for ai, bi in zip(a, b, strict=True))
    na = math.sqrt(sum(ai * ai for ai in a))
    nb = math.sqrt(sum(bi * bi for bi in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def find_dedup_candidate_pairs(
    vault: Vault,
    index: Index,
    *,
    min_cosine: float = DEFAULT_MIN_COSINE,
    neighbors_per_memory: int = DEFAULT_NEIGHBORS_PER_MEMORY,
    limit: int | None = None,
    max_memories: int | None = None,
) -> list[DedupCandidatePair]:
    """Walk the vault and report memory pairs whose first chunks are similar.

    For each memory with at least one stored chunk:

    1. Read the first chunk's embedding.
    2. Query ``index.query_vec`` for top ``neighbors_per_memory``
       neighbors (across all memories).
    3. For each neighbor that's not the same memory, compute true
       cosine from the raw vectors.
    4. If ``cosine >= min_cosine``, record the canonical pair.

    Pairs are deduplicated via canonical ordering (``a_id < b_id``)
    so each pair appears exactly once. The output is sorted by
    cosine descending so the strongest candidates appear first. When
    ``limit`` is set, only the top ``limit`` pairs are returned —
    handy for "show me the top 20 candidates" CLI use.

    **Cost.** ``limit`` only caps the *output*; the loop still issues
    one ``query_vec`` per memory in the index. ``query_vec`` is a vec0
    k-NN MATCH that scans the full ``memories_vec`` table, so the total
    work scales roughly as O(N²) in vault size — on a ~1k-memory vault
    this is several tens of seconds, on a 5k-memory vault it's minutes.
    For a bounded "preview" run, set ``max_memories`` to cap the outer
    loop at the first M memory ids (sorted lexicographically); the
    sweep then runs in O(M·N) and finishes in a few seconds. Production
    full scans should run async, not inside a smoke test with a 45-second
    timeout.

    The function is read-only on both vault and index. Failures to
    read individual memories are logged and skipped; the sweep does
    not abort on a single missing file.
    """
    # Get every memory_id that has at least one chunk vector. Any
    # memory without vectors is skipped — there's nothing to compare.
    with index._lock:
        rows = index.db.execute(
            """
            SELECT DISTINCT memory_id FROM memories_vec ORDER BY memory_id
            """
        ).fetchall()
    memory_ids = [r["memory_id"] for r in rows]

    # Outer-loop cap: lets the CLI ship a "preview / smoke" mode that
    # finishes in bounded time. The lexicographic sort above means the
    # subset is stable across runs.
    if max_memories is not None and max_memories >= 0:
        memory_ids = memory_ids[:max_memories]

    # Cache type/title/path for *all* indexed memories in one SQL — we
    # need the metadata for both the outer loop's memory_id and any
    # neighbors that come back from query_vec (those neighbors aren't
    # capped by max_memories). One query is cheap; N queries against
    # the metadata table dominate small vaults.
    metadata: dict[str, dict[str, str | None]] = {}
    with index._lock:
        meta_rows = index.db.execute("SELECT id, title, path, type FROM memories").fetchall()
    for row in meta_rows:
        metadata[row["id"]] = {
            "title": row["title"],
            "path": row["path"],
            "type": row["type"],
        }

    seen_pairs: dict[tuple[str, str], DedupCandidatePair] = {}
    for memory_id in memory_ids:
        emb_a = _read_chunk_embedding(index, memory_id, chunk_index=0)
        if emb_a is None:
            continue
        # Over-fetch a little so we have room to filter self-hits and
        # still come away with `neighbors_per_memory` candidates.
        try:
            vec_hits = index.query_vec(emb_a, limit=neighbors_per_memory + 1)
        except Exception as exc:
            logger.warning("dedup_candidates: vec query failed for %s: %s", memory_id, exc)
            continue
        for hit in vec_hits:
            other_id = hit.memory_id
            if other_id == memory_id:
                continue
            if other_id not in metadata:
                continue

            # Canonical pair ordering so a→b and b→a collapse.
            pair_key = tuple(sorted((memory_id, other_id)))
            assert len(pair_key) == 2
            if pair_key in seen_pairs:
                continue

            emb_b = _read_chunk_embedding(index, other_id, chunk_index=0)
            if emb_b is None:
                continue

            cosine = _cosine(emb_a, emb_b)
            if cosine < min_cosine:
                continue

            a_meta = metadata.get(pair_key[0]) or {}
            b_meta = metadata.get(pair_key[1]) or {}
            a_path = a_meta.get("path") or ""
            b_path = b_meta.get("path") or ""
            # Guard against vault-side path drift between index and
            # filesystem (the index could outlive a vault deletion).
            try:
                if a_path:
                    vault.read(a_path)
                if b_path:
                    vault.read(b_path)
            except MemoryNotFoundError:
                continue

            seen_pairs[pair_key] = DedupCandidatePair(
                a_id=pair_key[0],
                b_id=pair_key[1],
                cosine=cosine,
                a_title=a_meta.get("title"),
                b_title=b_meta.get("title"),
                a_path=a_path,
                b_path=b_path,
                a_type=str(a_meta.get("type") or ""),
                b_type=str(b_meta.get("type") or ""),
            )

    pairs = sorted(seen_pairs.values(), key=lambda p: p.cosine, reverse=True)
    if limit is not None:
        pairs = pairs[:limit]
    return pairs


__all__ = [
    "DEFAULT_MIN_COSINE",
    "DEFAULT_NEIGHBORS_PER_MEMORY",
    "DedupCandidatePair",
    "find_dedup_candidate_pairs",
]
