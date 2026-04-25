"""YAML frontmatter parsing, serialization, and validation.

Markdown files in the vault begin with a YAML frontmatter block. This module
is the single entry point for all frontmatter handling. Parsing returns raw
dicts so adapters and tools can be permissive on read; validation produces a
strongly-typed `Frontmatter` model conforming to `docs/frontmatter-spec.md`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

import frontmatter as fm
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryType(StrEnum):
    MEMORY = "memory"
    SKILL = "skill"
    SESSION = "session"
    DAILY = "daily"
    PERSON = "person"
    PROJECT = "project"
    DECISION = "decision"


class Confidence(StrEnum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    AMBIGUOUS = "ambiguous"


class Provenance(BaseModel):
    """Where an ingested memory came from."""

    model_config = ConfigDict(extra="allow")

    source: str
    ref: str | None = None
    ingested_at: datetime | None = None


class Frontmatter(BaseModel):
    """Validated frontmatter common to all memory files.

    Unknown fields are preserved (extra="allow") so the schema can evolve
    without breaking older files.
    """

    model_config = ConfigDict(extra="allow")

    id: UUID
    type: MemoryType
    created: datetime
    updated: datetime
    source: str

    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None
    confidence: Confidence | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    embedding_version: int | None = None
    deprecated_by: UUID | None = None

    scope: str | None = None
    prerequisites: list[str] = Field(default_factory=list)
    verification: str | None = None

    @model_validator(mode="after")
    def _require_skill_fields(self) -> Self:
        if self.type is MemoryType.SKILL:
            missing = [
                name for name in ("title", "scope", "verification") if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"type=skill requires fields: {', '.join(missing)}")
        return self


def parse(content: str) -> tuple[dict[str, Any], str]:
    """Split a frontmatter+markdown string into (metadata_dict, body).

    Files without frontmatter return ({}, original_content). YAML errors
    surface as the underlying YAML exception so callers can decide how to
    handle malformed files.
    """
    post = fm.loads(content)
    return dict(post.metadata), post.content


def serialize(metadata: dict[str, Any], body: str) -> str:
    """Render a metadata dict and markdown body back into frontmatter+markdown.

    Values must already be YAML-serializable (UUIDs as str, datetimes as
    datetime or ISO8601 str). Use `Frontmatter.model_dump(mode="json")` to
    get a serializer-safe dict from a validated model.
    """
    post = fm.Post(body, **metadata)
    rendered: str = fm.dumps(post)
    return rendered


def validate(metadata: dict[str, Any]) -> Frontmatter:
    """Validate a raw metadata dict against the Frontmatter schema.

    Raises pydantic.ValidationError on failure.
    """
    return Frontmatter.model_validate(metadata)
