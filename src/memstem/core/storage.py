"""Canonical markdown vault: read, write, walk, delete.

The vault is the source of truth. The SQLite index is derived and rebuildable.
This module is the only sanctioned write path; adapters and indexers must go
through it rather than touching files directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from memstem.core.frontmatter import Frontmatter, MemoryType, parse, serialize, validate

logger = logging.getLogger(__name__)

META_DIRNAME = "_meta"


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
        meta_dict, body = parse(text)
        try:
            fm_obj = validate(meta_dict)
        except PydanticValidationError as exc:
            raise InvalidFrontmatterError(f"{full}: {exc}") from exc
        rel = full.relative_to(self.root)
        return Memory(frontmatter=fm_obj, body=body, path=rel)

    def write(self, memory: Memory) -> None:
        full = self._resolve(memory.path)
        full.parent.mkdir(parents=True, exist_ok=True)
        meta_dict = memory.frontmatter.model_dump(mode="json", exclude_none=True)
        text = serialize(meta_dict, memory.body)
        full.write_text(text, encoding="utf-8")

    def delete(self, path: Path | str) -> None:
        full = self._resolve(path)
        if not full.is_file():
            raise MemoryNotFoundError(f"no memory at {full}")
        full.unlink()

    def walk(self, types: list[str] | None = None) -> Iterator[Memory]:
        """Yield every valid memory in the vault.

        Files under `_meta/` are skipped. Files with invalid frontmatter are
        logged and skipped, never raised — bulk operations should not crash on
        a single bad file.
        """
        meta_root = self.root / META_DIRNAME
        for md_path in sorted(self.root.rglob("*.md")):
            if meta_root in md_path.parents or md_path == meta_root:
                continue
            try:
                memory = self.read(md_path.relative_to(self.root))
            except InvalidFrontmatterError as exc:
                logger.warning("skipping %s: %s", md_path, exc)
                continue
            if types is not None and memory.type.value not in types:
                continue
            yield memory

    def _resolve(self, path: Path | str) -> Path:
        p = Path(path)
        full = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        if not full.is_relative_to(self.root):
            raise PathEscapesVaultError(f"path {full} is not inside vault root {self.root}")
        return full
