"""Tests for frontmatter parsing, serialization, and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from memstem.core.frontmatter import (
    Confidence,
    Frontmatter,
    MemoryType,
    Provenance,
    parse,
    serialize,
    validate,
)
from pydantic import ValidationError


def _minimal_metadata(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": str(uuid4()),
        "type": "memory",
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
    }
    base.update(overrides)
    return base


class TestParse:
    def test_parse_with_frontmatter(self) -> None:
        text = "---\nid: 1\ntitle: hi\n---\n\nbody here\n"
        meta, body = parse(text)
        assert meta == {"id": 1, "title": "hi"}
        assert body == "body here"

    def test_parse_without_frontmatter(self) -> None:
        meta, body = parse("just some markdown\n")
        assert meta == {}
        assert body == "just some markdown"

    def test_parse_empty_body(self) -> None:
        text = "---\ntitle: only meta\n---\n"
        meta, body = parse(text)
        assert meta == {"title": "only meta"}
        assert body == ""


class TestSerialize:
    def test_serialize_round_trip(self) -> None:
        meta = {"title": "round trip", "tags": ["a", "b"]}
        body = "body content\n\nmore body"
        rendered = serialize(meta, body)
        parsed_meta, parsed_body = parse(rendered)
        assert parsed_meta == meta
        assert parsed_body == body

    def test_serialize_pydantic_dump_round_trip(self) -> None:
        original = Frontmatter.model_validate(_minimal_metadata(title="test"))
        meta = original.model_dump(mode="json", exclude_none=True)
        rendered = serialize(meta, "hello")
        parsed_meta, _ = parse(rendered)
        revalidated = validate(parsed_meta)
        assert revalidated.id == original.id
        assert revalidated.title == "test"
        assert revalidated.type is MemoryType.MEMORY


class TestValidate:
    def test_minimal_memory(self) -> None:
        fm_obj = validate(_minimal_metadata())
        assert fm_obj.type is MemoryType.MEMORY
        assert isinstance(fm_obj.id, UUID)
        assert fm_obj.tags == []
        assert fm_obj.title is None

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate(_minimal_metadata(type="bogus"))

    def test_invalid_uuid_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate(_minimal_metadata(id="not-a-uuid"))

    def test_importance_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate(_minimal_metadata(importance=1.5))
        with pytest.raises(ValidationError):
            validate(_minimal_metadata(importance=-0.1))

    def test_skill_requires_title_scope_verification(self) -> None:
        with pytest.raises(ValidationError, match="type=skill requires"):
            validate(_minimal_metadata(type="skill"))

    def test_skill_with_required_fields_passes(self) -> None:
        fm_obj = validate(
            _minimal_metadata(
                type="skill",
                title="deploy to kinsta",
                scope="universal",
                verification="run health check",
            )
        )
        assert fm_obj.type is MemoryType.SKILL
        assert fm_obj.scope == "universal"

    def test_provenance_validates(self) -> None:
        fm_obj = validate(
            _minimal_metadata(
                provenance={
                    "source": "claude-code",
                    "ref": "session-abc",
                    "ingested_at": "2026-04-25T15:35:12+00:00",
                }
            )
        )
        assert isinstance(fm_obj.provenance, Provenance)
        assert fm_obj.provenance.source == "claude-code"

    def test_confidence_enum(self) -> None:
        fm_obj = validate(_minimal_metadata(confidence="extracted"))
        assert fm_obj.confidence is Confidence.EXTRACTED

    def test_extra_fields_preserved(self) -> None:
        fm_obj = validate(_minimal_metadata(future_field="future value"))
        assert fm_obj.model_dump()["future_field"] == "future value"

    def test_datetime_round_trip(self) -> None:
        ts = datetime(2026, 4, 25, 15, 0, tzinfo=UTC)
        fm_obj = validate(_minimal_metadata(created=ts.isoformat()))
        assert fm_obj.created == ts
