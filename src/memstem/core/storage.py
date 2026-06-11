"""Canonical markdown vault: read, write, walk, delete.

The vault is the source of truth. The SQLite index is derived and rebuildable.
This module is the only sanctioned write path; adapters and indexers must go
through it rather than touching files directly.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict

from memstem.core.frontmatter import Frontmatter, MemoryType, coerce, parse, serialize

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    The markdown vault is the canonical store (the SQLite index is derived and
    rebuildable), so a torn write here is unrecoverable data loss. Write to a
    sibling temp file on the same filesystem, fsync it, then ``os.replace`` —
    a crash leaves either the complete old file or the complete new one, never
    a truncated one. The directory is fsynced too so the rename survives a
    power loss.
    """
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


META_DIRNAME = "_meta"
# Operator-only directories — anything whose name starts with an underscore.
# These hold tickets, drafts, audit dumps, etc. that aren't memory files
# (no frontmatter, no schema). Scanners must skip them so a vault walk
# doesn't trip over operator artifacts. Examples currently in use:
#   _meta/     daemon-managed config, index, query log
#   _review/   skill collision review tickets (cleanup_retro, ADR 0012)
RESERVED_DIR_PREFIX = "_"


class VaultError(Exception):
    """Base exception for vault operations."""


class MemoryNotFoundError(VaultError):
    """Raised when a requested vault path does not exist."""


class InvalidFrontmatterError(VaultError):
    """Raised when a file's frontmatter fails schema validation."""


class PathEscapesVaultError(VaultError):
    """Raised when a path resolves outside the vault root."""


class Memory(BaseModel):
    """A memory file: validated frontmatter, markdown body, and vault path."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    frontmatter: Frontmatter
    body: str
    path: Path
    """Vault-relative path, e.g. `memories/people/brad.md`."""

    @property
    def id(self) -> UUID:
        return self.frontmatter.id

    @property
    def type(self) -> MemoryType:
        return self.frontmatter.type


class Vault:
    """Read/write access to a Memstem vault on disk."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def read(self, path: Path | str) -> Memory:
        full = self._resolve(path)
        if not full.is_file():
            raise MemoryNotFoundError(f"no memory at {full}")
        text = full.read_text(encoding="utf-8")
        rel = full.relative_to(self.root)
        try:
            meta_dict, body = parse(text)
        except yaml.YAMLError as exc:
            # Unparseable YAML can't be normalized — surface it so walk() skips
            # (and logs) the file rather than coercing garbage.
            raise InvalidFrontmatterError(f"{full}: {exc}") from exc
        # coerce(), not validate(): a file with missing or odd frontmatter is
        # normalized and indexed, never dropped. See frontmatter.coerce.
        fm_obj = coerce(meta_dict, path=str(rel))
        return Memory(frontmatter=fm_obj, body=body, path=rel)

    def write(self, memory: Memory) -> None:
        full = self._resolve(memory.path)
        full.parent.mkdir(parents=True, exist_ok=True)
        meta_dict = memory.frontmatter.model_dump(mode="json", exclude_none=True)
        text = serialize(meta_dict, memory.body)
        _atomic_write_text(full, text)

    def delete(self, path: Path | str) -> None:
        full = self._resolve(path)
        if not full.is_file():
            raise MemoryNotFoundError(f"no memory at {full}")
        full.unlink()

    def walk(self, types: list[str] | None = None) -> Iterator[Memory]:
        """Yield every valid memory in the vault.

        Files inside any directory whose name begins with an underscore are
        skipped (e.g. ``_meta/``, ``_review/``). Those are operator-only
        artifacts — daemon config, audit dumps, skill review tickets — not
        memory documents, and have no schema to validate against.

        Files with invalid frontmatter elsewhere are logged at WARNING and
        skipped — bulk operations should not crash on a single bad file, but
        unexpected schema breakage should still be visible to the operator.
        """
        for md_path in sorted(self.root.rglob("*.md")):
            if self._is_under_reserved_dir(md_path):
                continue
            try:
                memory = self.read(md_path.relative_to(self.root))
            except InvalidFrontmatterError as exc:
                logger.warning("skipping %s: %s", md_path, exc)
                continue
            if types is not None and memory.type.value not in types:
                continue
            yield memory

    def _is_under_reserved_dir(self, path: Path) -> bool:
        """True when any segment between the vault root and ``path`` starts with ``_``.

        The vault root itself is allowed to have leading underscores (we only
        check the parts *under* it). The check is one-shot: ``_meta`` at the
        top level, ``_review`` under ``skills/``, ``_drafts`` anywhere — all
        treated identically.
        """
        try:
            rel_parts = path.resolve().relative_to(self.root).parts
        except ValueError:
            return False
        # The file's own name is the last part; we only care about directory
        # segments. Stripping the file lets a top-level memory file like
        # `MEMORY.md` (no parent dir) work correctly.
        for segment in rel_parts[:-1]:
            if segment.startswith(RESERVED_DIR_PREFIX):
                return True
        return False

    def _resolve(self, path: Path | str) -> Path:
        p = Path(path)
        full = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        if not full.is_relative_to(self.root):
            raise PathEscapesVaultError(f"path {full} is not inside vault root {self.root}")
        return full
