"""Layer 1 of the dedup pipeline: exact-hash dedup at write time.

ADR 0012 (`docs/decisions/0012-llm-judge-dedup.md`) specifies a
three-layer dedup strategy. This module implements Layer 1 only — the
cheap, deterministic check that catches byte-identical duplicates
(modulo whitespace and case) for free, without any embedding or LLM
work.

Layers 2 (embedding candidate generation) and 3 (LLM-as-judge) are
implemented in later PRs.

The hash is computed over a *normalized* body — whitespace runs
collapsed to a single space, leading/trailing whitespace stripped,
lowercased — so trivial formatting changes don't bypass the check.
This is the documented behavior in ADR 0012's "Layer 1" section.

The mem0 audit (mem0ai/mem0#4573) catalogued one hallucinated fact
that re-entered memory 808 times via the recall feedback loop. A
single SHA-256 hash check would have collapsed all 808 to a single
record with `seen_count = 808`. That's the failure mode this layer
exists to prevent.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime

_WHITESPACE_RE = re.compile(r"\s+")


def normalized_body_hash(body: str) -> str:
    """Return the SHA-256 hex digest of a whitespace-normalized, lowercased body.

    Normalization rules:
    - Lowercase the entire body.
    - Collapse any run of whitespace (spaces, tabs, newlines) to a single space.
    - Strip leading and trailing whitespace.

    Two bodies that differ only in formatting produce the same hash; two bodies
    with different content produce different hashes with overwhelming probability
    (SHA-256 collision space is 2^256).
    """
    normalized = _WHITESPACE_RE.sub(" ", body.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_existing_memory_for_hash(db: sqlite3.Connection, body_hash: str) -> str | None:
    """Return the ``memory_id`` that already stores this body hash, or ``None``.

    Used by the pipeline to detect cross-record duplicates: a new record with a
    hash that matches an existing memory under a different ``(source, ref)``
    pair is skipped (Layer 1 dedup).
    """
    row = db.execute(
        "SELECT memory_id FROM body_hash_index WHERE body_hash = ?", (body_hash,)
    ).fetchone()
    return row["memory_id"] if row is not None else None


def increment_seen_count(db: sqlite3.Connection, body_hash: str) -> None:
    """Bump ``seen_count`` and ``last_seen`` on an existing row.

    Called when the pipeline drops a duplicate so the audit trail can answer
    "how many times did the same content try to enter the index?"
    """
    now = datetime.now(tz=UTC).isoformat()
    db.execute(
        """
        UPDATE body_hash_index
        SET seen_count = seen_count + 1, last_seen = ?
        WHERE body_hash = ?
        """,
        (now, body_hash),
    )


def record_body_hash(db: sqlite3.Connection, body_hash: str, memory_id: str) -> None:
    """Insert or update the ``hash → memory_id`` mapping for this body.

    Behavior:
    - Stale entries for ``memory_id`` under a *different* hash are deleted
      first. This handles the body-edit case: a memory whose body changed
      should not leave its old hash dangling in the index, where it could
      false-positive a future record bearing that old content.
    - The new row is inserted with ``seen_count = 1``, or — if this exact
      hash already maps to this same ``memory_id`` (idempotent re-emit) —
      its counter is bumped via ``ON CONFLICT DO UPDATE``.
    """
    now = datetime.now(tz=UTC).isoformat()

    # Drop stale rows for this memory under any other hash (body-edit cleanup).
    db.execute(
        "DELETE FROM body_hash_index WHERE memory_id = ? AND body_hash != ?",
        (memory_id, body_hash),
    )

    db.execute(
        """
        INSERT INTO body_hash_index(body_hash, memory_id, seen_count, last_seen)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(body_hash) DO UPDATE SET
            seen_count = seen_count + 1,
            last_seen = excluded.last_seen
        """,
        (body_hash, memory_id, now),
    )


__all__ = [
    "find_existing_memory_for_hash",
    "increment_seen_count",
    "normalized_body_hash",
    "record_body_hash",
]
