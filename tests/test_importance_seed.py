"""Tests for the heuristic importance seed (ADR 0008 Tier 1 PR-A)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memstem.core.frontmatter import MemoryType
from memstem.core.importance_seed import (
    DEFAULT_TYPE_WEIGHT,
    LENGTH_PENALTY,
    LENGTH_THRESHOLD_CHARS,
    MAX_CEILING,
    MIN_FLOOR,
    RECENCY_DECAY_DAYS,
    RECENCY_FLOOR,
    RECENCY_MAX,
    TYPE_WEIGHTS,
    compute_seed,
)

# ─── Type weights ────────────────────────────────────────────────────


def test_type_weights_match_adr_0008() -> None:
    """Sanity-check the published weights against ADR 0008 Tier 1."""
    assert TYPE_WEIGHTS[MemoryType.SKILL] == 0.7
    assert TYPE_WEIGHTS[MemoryType.DECISION] == 0.6
    assert TYPE_WEIGHTS[MemoryType.MEMORY] == 0.5
    assert TYPE_WEIGHTS[MemoryType.SESSION] == 0.3
    assert TYPE_WEIGHTS[MemoryType.DISTILLATION] == 0.7


def test_default_type_weight_for_unknown_string() -> None:
    """Unknown type strings fall back to the neutral default."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = compute_seed(
        memory_type="unknown_future_type",
        body_length=500,
        created=now,
        now=now,
    )
    # Fresh + ample length: base = DEFAULT_TYPE_WEIGHT (0.5)
    # plus full recency contribution (RECENCY_WEIGHT * (RECENCY_MAX -
    # RECENCY_FLOOR) = 0.3 * 0.5 = 0.15) = 0.65.
    assert score == pytest.approx(DEFAULT_TYPE_WEIGHT + 0.15)


# ─── Recency ─────────────────────────────────────────────────────────


def test_recency_full_for_fresh_memory() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=500,
        created=now,  # age = 0
        now=now,
    )
    # base 0.5 + recency contribution at max recency (RECENCY_WEIGHT *
    # (RECENCY_MAX - RECENCY_FLOOR)) = 0.5 + 0.3 * 0.5 = 0.65
    assert score == pytest.approx(0.65)


def test_recency_floor_for_old_memory() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    created = now - timedelta(days=RECENCY_DECAY_DAYS + 30)  # well past decay window
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=500,
        created=created,
        now=now,
    )
    # base 0.5 + recency contribution at floor = 0.5 + 0.3 * 0 = 0.5
    assert score == pytest.approx(0.5)


def test_recency_midway_decay() -> None:
    """Halfway through the decay window: recency contribution is mid-range."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    created = now - timedelta(days=RECENCY_DECAY_DAYS / 2)
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=500,
        created=created,
        now=now,
    )
    # Recency contribution: RECENCY_WEIGHT * ( (RECENCY_MAX+RECENCY_FLOOR)/2 - RECENCY_FLOOR )
    expected_recency = 0.3 * ((RECENCY_MAX + RECENCY_FLOOR) / 2 - RECENCY_FLOOR)
    assert score == pytest.approx(0.5 + expected_recency)


def test_future_created_treated_as_age_zero() -> None:
    """Negative ages (clock skew, future-dated record) treat as fresh."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    created = now + timedelta(days=10)
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=500,
        created=created,
        now=now,
    )
    # Same as age=0 (fresh).
    assert score == pytest.approx(0.65)


# ─── Length penalty ──────────────────────────────────────────────────


def test_short_body_penalty() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=LENGTH_THRESHOLD_CHARS - 1,
        created=now,
        now=now,
    )
    # base 0.5 + recency 0.15 - length penalty 0.1 = 0.55
    assert score == pytest.approx(0.65 - LENGTH_PENALTY)


def test_long_body_no_penalty() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = compute_seed(
        memory_type=MemoryType.MEMORY,
        body_length=LENGTH_THRESHOLD_CHARS + 1,
        created=now,
        now=now,
    )
    assert score == pytest.approx(0.65)


# ─── Type combos ─────────────────────────────────────────────────────


def test_skill_outranks_session_for_same_age_and_length() -> None:
    """Type weight is the dominant signal in the seed."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    skill = compute_seed(memory_type=MemoryType.SKILL, body_length=500, created=now, now=now)
    session = compute_seed(memory_type=MemoryType.SESSION, body_length=500, created=now, now=now)
    assert skill > session
    assert skill == pytest.approx(0.85)
    assert session == pytest.approx(0.45)


def test_distillation_matches_skill_weight() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    skill = compute_seed(memory_type=MemoryType.SKILL, body_length=500, created=now, now=now)
    distillation = compute_seed(
        memory_type=MemoryType.DISTILLATION, body_length=500, created=now, now=now
    )
    assert skill == distillation


# ─── Bounds ──────────────────────────────────────────────────────────


def test_score_clamped_to_floor() -> None:
    """A degenerate record (unknown type, tiny body, ancient) clamps to MIN_FLOOR."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    created = now - timedelta(days=10000)
    score = compute_seed(
        memory_type="unknown",
        body_length=1,
        created=created,
        now=now,
    )
    # base 0.5 + 0 (old) - 0.1 (short) = 0.4. Above MIN_FLOOR; check we
    # never drop below.
    assert score >= MIN_FLOOR


def test_score_clamped_to_ceiling() -> None:
    """A maximum-signal record stays under MAX_CEILING."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = compute_seed(memory_type=MemoryType.SKILL, body_length=10000, created=now, now=now)
    assert score <= MAX_CEILING
    # 0.7 + 0.15 = 0.85 — well under the ceiling.


def test_explicit_floor_when_signals_negative() -> None:
    """Construct a case where raw signals would go below MIN_FLOOR.

    A record can't realistically score below MIN_FLOOR with the
    current weights — minimum type weight is 0.3 (session), recency
    floor adds 0, and length penalty subtracts 0.1, for a worst case
    of 0.2 which is still above MIN_FLOOR. This test verifies the
    floor still applies should weights ever change.
    """
    # Force a weight where the raw computation would fall below MIN_FLOOR.
    # Currently the worst real case is 0.3 - 0.1 = 0.2 > MIN_FLOOR (0.1),
    # so we can't directly trigger the clamp through the public API.
    # Instead, sanity-check that the function never returns below the floor.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for memory_type in (MemoryType.SESSION, MemoryType.DAILY, MemoryType.MEMORY):
        for body_length in (0, 50, 500):
            for age_days in (0, 30, 90, 365):
                score = compute_seed(
                    memory_type=memory_type,
                    body_length=body_length,
                    created=now - timedelta(days=age_days),
                    now=now,
                )
                assert MIN_FLOOR <= score <= MAX_CEILING, (
                    f"out of bounds: type={memory_type} length={body_length} "
                    f"age={age_days} score={score}"
                )


# ─── Default `now` argument ─────────────────────────────────────────


def test_default_now_uses_current_time() -> None:
    """Without an explicit ``now``, the function uses datetime.now(tz=UTC)."""
    # Just verify it doesn't crash and returns something in-bounds.
    score = compute_seed(
        memory_type=MemoryType.SKILL,
        body_length=500,
        created=datetime.now(tz=UTC),
    )
    assert MIN_FLOOR <= score <= MAX_CEILING
