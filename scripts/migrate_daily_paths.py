#!/usr/bin/env python3
"""One-shot relocation of `daily` memories onto their ADR-0033 paths.

`Pipeline.process` reuses an existing memory's path on re-ingest, so the
ADR-0033 fix only governs *new* daily records. Everything already in the
vault keeps the slot it was mis-assigned by the old
`daily/<agent>/<created-date>.md` rule — where `created` degraded to the
source file's mtime in UTC (shifting evening edits onto the next day) and
every dated file in the workspace competed for one slot per date.

This script recomputes each daily memory's correct path from its *source
ref* and moves it there, keeping the memory id so links, embeddings and
distillation references survive. It does not re-ingest: freeing a slot is
enough, the next daemon reconcile refills it from the source file that had
lost it.

    python scripts/migrate_daily_paths.py --vault ~/memstem-vault          # dry run
    python scripts/migrate_daily_paths.py --vault ~/memstem-vault --apply

Stop the daemon before running with --apply.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

import yaml

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Directories an agent workspace keeps dated memory files under. A ref's
# path relative to the first of these it sits beneath becomes its scope.
MEMORY_DIR_NAMES = ("memory", "memories", "notes")


def _frontmatter(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        loaded = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _ref_for(db: sqlite3.Connection, memory_id: str, vault: Path, rel_path: str) -> str | None:
    """Source ref for a memory: record_map first, frontmatter provenance second.

    Repeated overwrites of a shared slot orphaned some record_map rows, so
    the on-disk provenance block is the fallback — and per the project's
    markdown-canonical invariant, it is the more trustworthy of the two.
    """
    row = db.execute("SELECT ref FROM record_map WHERE memory_id = ?", (memory_id,)).fetchone()
    if row and row[0]:
        return str(row[0])
    provenance = _frontmatter(vault / rel_path).get("provenance")
    if isinstance(provenance, dict) and provenance.get("ref"):
        return str(provenance["ref"])
    return None


def _agent_of(db: sqlite3.Connection, memory_id: str) -> str | None:
    for (tag,) in db.execute("SELECT tag FROM tags WHERE memory_id = ?", (memory_id,)):
        if isinstance(tag, str) and tag.startswith("agent:"):
            stripped = tag[len("agent:") :].strip()
            if stripped:
                return stripped
    return None


def _correct_path(ref: str, agent: str | None) -> str | None:
    """The ADR-0033 path for a daily memory whose source file is `ref`."""
    source = Path(ref)
    if not DATE_RE.match(source.stem):
        return None

    scope = ""
    parts = source.parent.parts
    for name in MEMORY_DIR_NAMES:
        if name in parts:
            idx = len(parts) - 1 - parts[::-1].index(name)
            scope = "/".join(parts[idx + 1 :])
            break

    segments = ["daily"]
    if agent:
        segments.append(agent)
    if scope:
        segments.append(scope)
    segments.append(f"{source.stem}.md")
    return "/".join(segments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="perform the moves")
    args = parser.parse_args()

    vault: Path = args.vault.expanduser().resolve()
    db_path = vault / "_meta" / "index.db"
    if not db_path.is_file():
        print(f"no index at {db_path}", file=sys.stderr)
        return 1

    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT id, path FROM memories WHERE type = 'daily' AND deleted_at IS NULL"
    ).fetchall()

    # `memories.path` is UNIQUE and the constraint does not care about
    # `deleted_at`, so a tombstoned row still holds its slot. Seed `taken`
    # from every row in the table, not just the live daily ones, or a
    # rename resolves cleanly here and then fails on the UPDATE.
    taken = dict(db.execute("SELECT path, id FROM memories"))
    moves: list[tuple[str, str, str]] = []
    unchanged = 0
    skipped: list[tuple[str, str]] = []

    owns_ref = dict(db.execute("SELECT memory_id, ref FROM record_map WHERE source = 'openclaw'"))
    refs: dict[str, str] = {}
    evictions: list[tuple[str, str]] = []

    pending: list[tuple[str, str, str]] = []
    for memory_id, rel_path in rows:
        ref = _ref_for(db, memory_id, vault, rel_path)
        if not ref:
            skipped.append((rel_path, "no source ref"))
            continue
        refs[memory_id] = ref
        target = _correct_path(ref, _agent_of(db, memory_id))
        if target is None:
            skipped.append((rel_path, f"ref is not a dated file: {ref}"))
            continue
        if target == rel_path:
            unchanged += 1
            continue
        pending.append((memory_id, rel_path, target))

    def _is_superseded_duplicate(occupant: str, mover: str) -> bool:
        """True if `occupant` is a stale copy that `mover` legitimately replaces.

        Repeated overwrites of a shared slot left orphans: a memory whose
        `record_map` row was reassigned to a newer id for the *same* source
        file. Only that exact shape is evictable — an occupant that still
        owns its ref is a real record and blocks the move.
        """
        return occupant not in owns_ref and refs.get(occupant) == refs.get(mover)

    # The mtime bug shifted whole runs of days by one, so targets form
    # chains: 06-18 wants 06-17, whose occupant wants 06-16, and so on.
    # Resolve to a fixed point — each move frees the slot the next one in
    # the chain is waiting on. Applying in this order keeps every rename
    # landing on an empty target.
    while pending:
        progressed = False
        blocked: list[tuple[str, str, str]] = []
        for memory_id, rel_path, target in pending:
            occupant = taken.get(target)
            if occupant is not None and occupant != memory_id:
                if not _is_superseded_duplicate(occupant, memory_id):
                    blocked.append((memory_id, rel_path, target))
                    continue
                evictions.append((occupant, target))
                taken.pop(target, None)
            moves.append((memory_id, rel_path, target))
            taken.pop(rel_path, None)
            taken[target] = memory_id
            progressed = True
        pending = blocked
        if not progressed:
            for _, rel_path, target in pending:
                skipped.append((rel_path, f"target {target} held by {taken.get(target)}"))
            break

    print(f"daily memories: {len(rows)}")
    print(f"  already correct : {unchanged}")
    print(f"  to relocate     : {len(moves)}")
    print(f"  superseded dups : {len(evictions)}")
    print(f"  skipped         : {len(skipped)}")
    for _, path in evictions[:10]:
        print(f"    evict {path}")
    for rel_path, reason in skipped[:20]:
        print(f"    - {rel_path}: {reason}")
    if len(skipped) > 20:
        print(f"    ... and {len(skipped) - 20} more")
    for _, old, new in moves[:20]:
        print(f"    {old}  ->  {new}")
    if len(moves) > 20:
        print(f"    ... and {len(moves) - 20} more")

    if not args.apply:
        print("\ndry run — re-run with --apply to perform the moves")
        return 0

    # Evict first so the slots are free before the renames land. Nothing is
    # hard-deleted: the file is parked under _meta/ and the row is tombstoned
    # with `deleted_at`, matching how ADR 0026 retires a memory.
    quarantine = vault / "_meta" / "superseded-daily"
    for memory_id, rel_path in evictions:
        stale = vault / rel_path
        parked_rel = f"_meta/superseded-daily/{rel_path}"
        if stale.is_file():
            parked = quarantine / rel_path
            parked.parent.mkdir(parents=True, exist_ok=True)
            stale.rename(parked)
        # The row's path moves with the file: `memories.path` is UNIQUE
        # regardless of `deleted_at`, so tombstoning alone would leave the
        # slot locked against the record that is about to claim it.
        db.execute(
            "UPDATE memories SET deleted_at = datetime('now'), path = ? WHERE id = ?",
            (parked_rel, memory_id),
        )
    if evictions:
        print(f"parked {len(evictions)} superseded duplicates under {quarantine}")

    db.commit()

    # Commit per move so an unexpected failure leaves a consistent prefix
    # rather than a vault whose files moved and whose index says otherwise.
    # The script recomputes desired state from source refs, so re-running
    # after a partial run converges.
    moved = 0
    for memory_id, old, new in moves:
        src, dst = vault / old, vault / new
        renamed = False
        if not src.is_file():
            # Already relocated by an earlier partial run: index-only update.
            print(f"  ! source file missing, index-only update: {old}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            renamed = True
        try:
            db.execute("UPDATE memories SET path = ? WHERE id = ?", (new, memory_id))
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            if renamed:
                dst.rename(src)
            print(f"\nstopped at {old} -> {new}: {exc}", file=sys.stderr)
            print(f"relocated {moved} before stopping; re-run to continue", file=sys.stderr)
            return 1
        moved += 1
    print(f"\nrelocated {moved} daily memories")
    print("restart the daemon — reconcile will refill the freed slots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
