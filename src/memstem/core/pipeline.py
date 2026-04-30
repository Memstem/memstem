"""Record → Memory ingestion pipeline.

Adapters emit `MemoryRecord` objects describing what they saw on disk.
The pipeline turns each record into a canonical `Memory`: writes the
markdown file, upserts the index, and (if an embedder is configured)
chunks the body, embeds each chunk, and stores the vectors.

Identity is stable per `(source, ref)`. We store the mapping from a
record's source ref to its assigned memory id in a small SQLite table
so that re-emits of the same record (which adapters do on file change)
update the same Memory instead of creating duplicates.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from memstem.adapters.base import MemoryRecord
from memstem.core.dedup import (
    find_existing_memory_for_hash,
    increment_seen_count,
    normalized_body_hash,
    record_body_hash,
)
from memstem.core.extraction import NoiseAction, noise_filter
from memstem.core.frontmatter import Frontmatter, MemoryType, validate
from memstem.core.importance_seed import compute_seed
from memstem.core.index import Index, body_hash
from memstem.core.storage import Memory, MemoryNotFoundError, Vault

logger = logging.getLogger(__name__)


def _ensure_record_map(db: sqlite3.Connection) -> None:
    """Idempotently create the `record_map` table for source-ref → id lookup."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS record_map (
            source TEXT NOT NULL,
            ref TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            PRIMARY KEY (source, ref)
        )
        """
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _agent_tag(record: MemoryRecord) -> str | None:
    """Extract `agent:<tag>` from record tags, returning the tag or None.

    Used to disambiguate paths for skills and daily logs, which would
    otherwise collide across agents (every agent has a 2026-04-26 daily
    log; many agents share skill names like "deploy" or "voice-sms").
    """
    for tag in record.tags:
        if tag.startswith("agent:"):
            stripped = tag[len("agent:") :].strip()
            if stripped:
                return stripped
    return None


def _path_for_memory(fm: Frontmatter, record: MemoryRecord) -> Path:
    agent = _agent_tag(record)
    if fm.type is MemoryType.SKILL:
        slug_source = fm.title or str(fm.id)
        slug = slug_source.lower().replace(" ", "-")[:64]
        if agent:
            return Path(f"skills/{agent}/{slug}.md")
        return Path(f"skills/{slug}.md")
    if fm.type is MemoryType.SESSION:
        session_id = record.metadata.get("session_id") or str(fm.id)
        return Path(f"sessions/{session_id}.md")
    if fm.type is MemoryType.DAILY:
        date = fm.created.date().isoformat()
        if agent:
            return Path(f"daily/{agent}/{date}.md")
        return Path(f"daily/{date}.md")
    if agent:
        return Path(f"memories/{record.source}/{agent}/{fm.id}.md")
    return Path(f"memories/{record.source}/{fm.id}.md")


class Pipeline:
    """Convert adapter `MemoryRecord` objects into canonical `Memory` writes."""

    def __init__(
        self,
        vault: Vault,
        index: Index,
        embedding_signature: str = "",
        boot_echo_hashes: frozenset[str] | None = None,
    ) -> None:
        self.vault = vault
        self.index = index
        self.embedding_signature = embedding_signature
        self.boot_echo_hashes = boot_echo_hashes
        _ensure_record_map(self.index.db)

    def process(self, record: MemoryRecord) -> Memory | None:
        """Persist one record as a canonical Memory; idempotent for re-emits.

        Returns ``None`` if the record was filtered out as noise (ADR
        0011) or detected as an exact-body duplicate (ADR 0012, Layer
        1). Callers that already discarded the result keep working
        unchanged; callers that use the result must accept ``None``.

        The pipeline is fast-path only: it writes the markdown file, the
        memories/tags/links/FTS5 rows, and (if needed) enqueues the
        record for embedding. The actual vector embedding happens
        asynchronously in :class:`memstem.core.embed_worker.EmbedWorker`.
        This keeps ingestion latency bounded by disk I/O and SQLite —
        not by an embedder that may be CPU-bound or rate-limited.

        Re-emits with unchanged body and matching embedder signature
        skip the enqueue: the existing vectors are still valid and
        re-embedding would just burn rate-limit quota. Body or signature
        changes (or the absence of vectors) still enqueue.
        """
        decision = noise_filter(record, boot_echo_hashes=self.boot_echo_hashes)
        if decision.action is NoiseAction.DROP:
            logger.info(
                "noise filter dropped record (source=%s, ref=%s, kind=%s): %s",
                record.source,
                record.ref,
                decision.kind,
                decision.reason,
            )
            return None

        # TAG_TRANSIENT: persist the record but stamp `valid_to` so the
        # search layer auto-expires it after the documented TTL.
        transient_valid_to: datetime | None = None
        if decision.action is NoiseAction.TAG_TRANSIENT:
            ttl_days = decision.ttl_days or 28
            transient_valid_to = datetime.now(tz=UTC) + timedelta(days=ttl_days)
            logger.info(
                "noise filter tagged transient (source=%s, ref=%s, kind=%s, valid_to=%s): %s",
                record.source,
                record.ref,
                decision.kind,
                transient_valid_to.isoformat(),
                decision.reason,
            )

        # ADR 0012, Layer 1: exact-body dedup. The hash check catches the
        # recall feedback loop (mem0's 808-copy failure mode) for free.
        # We check BEFORE assigning a memory_id so a true duplicate doesn't
        # leave a record_map entry that would fight with the canonical one.
        body_dedup_hash = normalized_body_hash(record.body)
        existing_id_for_ref = self._lookup_id_or_none(record.source, record.ref)
        existing_id_for_hash = find_existing_memory_for_hash(self.index.db, body_dedup_hash)
        is_cross_record_duplicate = existing_id_for_hash is not None and (
            existing_id_for_ref is None or existing_id_for_hash != str(existing_id_for_ref)
        )
        if is_cross_record_duplicate:
            with self.index._lock, self.index.db:
                increment_seen_count(self.index.db, body_dedup_hash)
            logger.info(
                "dedup: skipped exact body duplicate (source=%s, ref=%s, points_to=%s)",
                record.source,
                record.ref,
                existing_id_for_hash,
            )
            return None

        memory_id = existing_id_for_ref or uuid4()
        fm = self._build_frontmatter(record, memory_id, valid_to=transient_valid_to)
        path = self._existing_path(memory_id) or _path_for_memory(fm, record)
        memory = Memory(frontmatter=fm, body=record.body, path=path)

        self.vault.write(memory)
        self.index.upsert(memory)
        self._record_mapping(record.source, record.ref, memory_id)
        with self.index._lock, self.index.db:
            record_body_hash(self.index.db, body_dedup_hash, str(memory_id))
        if self.index.needs_reembed(
            str(memory_id), body_hash(record.body), self.embedding_signature
        ):
            self.index.enqueue_embed(str(memory_id))
        return memory

    def _lookup_id_or_none(self, source: str, ref: str) -> UUID | None:
        row = self.index.db.execute(
            "SELECT memory_id FROM record_map WHERE source = ? AND ref = ?",
            (source, ref),
        ).fetchone()
        return UUID(row["memory_id"]) if row is not None else None

    def _lookup_or_assign_id(self, source: str, ref: str) -> UUID:
        return self._lookup_id_or_none(source, ref) or uuid4()

    def _existing_path(self, memory_id: UUID) -> Path | None:
        row = self.index.db.execute(
            "SELECT path FROM memories WHERE id = ?", (str(memory_id),)
        ).fetchone()
        if row is None:
            return None
        # Confirm the on-disk file still exists; fall back to a fresh path if not.
        try:
            self.vault.read(row["path"])
            return Path(row["path"])
        except MemoryNotFoundError:
            return None

    def _build_frontmatter(
        self,
        record: MemoryRecord,
        memory_id: UUID,
        valid_to: datetime | None = None,
    ) -> Frontmatter:
        meta = dict(record.metadata)
        type_str = meta.get("type", "memory")
        created = _parse_iso(meta.get("created")) or datetime.now(tz=UTC)
        updated = _parse_iso(meta.get("updated")) or datetime.now(tz=UTC)
        provenance = {
            "source": record.source,
            "ref": record.ref,
            "ingested_at": datetime.now(tz=UTC).isoformat(),
        }
        payload: dict[str, Any] = {
            "id": str(memory_id),
            "type": type_str,
            "created": created.isoformat(),
            "updated": updated.isoformat(),
            "source": record.source,
            "title": record.title,
            "tags": list(record.tags),
            "provenance": provenance,
        }
        if valid_to is not None:
            payload["valid_to"] = valid_to.isoformat()
        # Skill-typed records need scope+verification; default to permissive.
        if type_str == "skill":
            raw_fm = (
                meta.get("raw_frontmatter") if isinstance(meta.get("raw_frontmatter"), dict) else {}
            )
            assert isinstance(raw_fm, dict)
            payload.setdefault("scope", str(raw_fm.get("scope") or "universal"))
            payload.setdefault("verification", str(raw_fm.get("verification") or "verify by hand"))
        # ADR 0008 Tier 1 PR-A: seed `importance` from cheap heuristics
        # at ingest. We only set it when the record doesn't already
        # carry one — user-set or upstream-set values are preserved.
        # The search-side multiplier (`final = rrf * (1 + alpha *
        # importance)`) is inert without this until pinned/bumped.
        if "importance" not in meta:
            payload["importance"] = compute_seed(
                memory_type=type_str,
                body_length=len(record.body),
                created=created,
            )
        else:
            payload["importance"] = meta["importance"]
        return validate(payload)

    def _record_mapping(self, source: str, ref: str, memory_id: UUID) -> None:
        with self.index._lock, self.index.db:
            self.index.db.execute(
                """
                INSERT OR REPLACE INTO record_map(source, ref, memory_id)
                VALUES (?, ?, ?)
                """,
                (source, ref, str(memory_id)),
            )


__all__ = ["Pipeline"]
