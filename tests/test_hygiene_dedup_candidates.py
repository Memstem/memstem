"""Tests for `memstem.hygiene.dedup_candidates` (ADR 0012 Layer 2)."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.hygiene.dedup_candidates import (
    DEFAULT_MIN_COSINE,
    DedupCandidatePair,
    find_dedup_candidate_pairs,
)


def _normalized_random(seed: int, dim: int = 768) -> list[float]:
    """Return a random unit vector. Deterministic given seed."""
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    if norm == 0.0:
        return raw
    return [x / norm for x in raw]


def _make_memory(
    *,
    body: str,
    vault: Vault,
    title: str | None = None,
    type_: str = "memory",
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title or "untitled",
        "tags": [],
    }
    if type_ == "skill":
        metadata["scope"] = "universal"
        metadata["verification"] = "verify by hand"
    fm = validate(metadata)
    if type_ == "skill":
        path = Path(f"skills/{fm.id}.md")
    else:
        path = Path(f"memories/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    return memory


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    idx = Index(tmp_path / "index.db", dimensions=768)
    idx.connect()
    yield idx
    idx.close()


class TestEmptyState:
    def test_no_memories_returns_empty(self, vault: Vault, index: Index) -> None:
        assert find_dedup_candidate_pairs(vault, index) == []

    def test_no_vectors_returns_empty(self, vault: Vault, index: Index) -> None:
        # A memory in the index but with no chunks gets skipped.
        m = _make_memory(body="alpha", vault=vault)
        index.upsert(m)
        assert find_dedup_candidate_pairs(vault, index) == []


class TestVecCandidatePairs:
    def test_near_identical_vectors_pair(self, vault: Vault, index: Index) -> None:
        # Two memories with near-identical vectors: cosine ≈ 1.0.
        a = _make_memory(body="alpha", vault=vault, title="A")
        b = _make_memory(body="alpha-twin", vault=vault, title="B")
        index.upsert(a)
        index.upsert(b)
        v_a = _normalized_random(42)
        # b's vector is `a` plus a tiny noise — cosine still very close to 1.
        rng = random.Random(99)
        noise = [rng.gauss(0.0, 0.001) for _ in range(768)]
        v_b = [x + n for x, n in zip(v_a, noise, strict=True)]
        norm = math.sqrt(sum(x * x for x in v_b))
        v_b = [x / norm for x in v_b]
        index.upsert_vectors(str(a.id), ["a"], [v_a])
        index.upsert_vectors(str(b.id), ["b"], [v_b])

        pairs = find_dedup_candidate_pairs(vault, index)
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.cosine > 0.99
        # Canonical ordering.
        assert pair.a_id < pair.b_id

    def test_unrelated_vectors_no_pair(self, vault: Vault, index: Index) -> None:
        # Two random vectors typically have cosine ~0 in 768 dims —
        # well below the 0.85 threshold.
        a = _make_memory(body="alpha", vault=vault)
        b = _make_memory(body="beta", vault=vault)
        index.upsert(a)
        index.upsert(b)
        index.upsert_vectors(str(a.id), ["a"], [_normalized_random(1)])
        index.upsert_vectors(str(b.id), ["b"], [_normalized_random(2)])
        pairs = find_dedup_candidate_pairs(vault, index)
        assert pairs == []

    def test_threshold_can_be_relaxed(self, vault: Vault, index: Index) -> None:
        # With min_cosine=0 we accept any pair (still excludes self).
        a = _make_memory(body="alpha", vault=vault)
        b = _make_memory(body="beta", vault=vault)
        index.upsert(a)
        index.upsert(b)
        index.upsert_vectors(str(a.id), ["a"], [_normalized_random(1)])
        index.upsert_vectors(str(b.id), ["b"], [_normalized_random(2)])
        pairs = find_dedup_candidate_pairs(vault, index, min_cosine=-1.0)
        assert len(pairs) == 1

    def test_threshold_filter_strictness(self, vault: Vault, index: Index) -> None:
        # A pair with cosine ~0.9 should appear at min_cosine=0.85 but not at 0.95.
        a = _make_memory(body="alpha", vault=vault)
        b = _make_memory(body="beta", vault=vault)
        index.upsert(a)
        index.upsert(b)
        v_a = _normalized_random(1)
        # Construct v_b = 0.9 * v_a + small orthogonal noise. Then
        # normalize. Cosine ≈ 0.9.
        rng = random.Random(99)
        noise_raw = [rng.gauss(0.0, 1.0) for _ in range(768)]
        # Project out the v_a component so noise is orthogonal-ish.
        proj = sum(x * y for x, y in zip(v_a, noise_raw, strict=True))
        noise = [n - proj * a_i for n, a_i in zip(noise_raw, v_a, strict=True)]
        # Normalize noise.
        n_norm = math.sqrt(sum(x * x for x in noise))
        noise_unit = [x / n_norm for x in noise]
        # Mix with target cosine 0.9.
        target_cos = 0.9
        sin = math.sqrt(1.0 - target_cos * target_cos)
        v_b = [target_cos * a_i + sin * n_i for a_i, n_i in zip(v_a, noise_unit, strict=True)]
        # v_b is already unit norm by construction, but normalize for safety.
        nb = math.sqrt(sum(x * x for x in v_b))
        v_b = [x / nb for x in v_b]

        index.upsert_vectors(str(a.id), ["a"], [v_a])
        index.upsert_vectors(str(b.id), ["b"], [v_b])

        pairs_loose = find_dedup_candidate_pairs(vault, index, min_cosine=0.85)
        assert len(pairs_loose) == 1
        pairs_strict = find_dedup_candidate_pairs(vault, index, min_cosine=0.95)
        assert pairs_strict == []

    def test_self_hit_filtered(self, vault: Vault, index: Index) -> None:
        # A memory shouldn't pair with itself even though its vec
        # query naturally returns itself first.
        a = _make_memory(body="alpha", vault=vault)
        index.upsert(a)
        index.upsert_vectors(str(a.id), ["a"], [_normalized_random(1)])
        pairs = find_dedup_candidate_pairs(vault, index, min_cosine=-1.0)
        assert pairs == []


class TestPairCanonicalization:
    def test_ab_and_ba_collapse_to_one_pair(self, vault: Vault, index: Index) -> None:
        # Even though both a and b query for neighbors and "find each
        # other," the pair is reported once.
        a = _make_memory(body="alpha", vault=vault)
        b = _make_memory(body="alpha-twin", vault=vault)
        index.upsert(a)
        index.upsert(b)
        v = _normalized_random(7)
        index.upsert_vectors(str(a.id), ["a"], [v])
        index.upsert_vectors(str(b.id), ["b"], [v])  # identical vec
        pairs = find_dedup_candidate_pairs(vault, index)
        assert len(pairs) == 1


class TestSkillFlag:
    def test_skill_pair_is_flagged(self, vault: Vault, index: Index) -> None:
        a = _make_memory(body="alpha", vault=vault, type_="skill", title="alpha-skill")
        b = _make_memory(body="alpha", vault=vault, title="alpha-memory")
        index.upsert(a)
        index.upsert(b)
        v = _normalized_random(7)
        index.upsert_vectors(str(a.id), ["a"], [v])
        index.upsert_vectors(str(b.id), ["b"], [v])
        pairs = find_dedup_candidate_pairs(vault, index)
        assert len(pairs) == 1
        assert pairs[0].involves_skill is True


class TestSorting:
    def test_pairs_sorted_by_cosine_descending(self, vault: Vault, index: Index) -> None:
        # Three pairs with different similarity levels. The strongest
        # match should appear first.
        v_base = _normalized_random(1)

        # Tighter pair: cosine ~ 0.99
        a1 = _make_memory(body="x", vault=vault)
        b1 = _make_memory(body="y", vault=vault)
        rng = random.Random(2)
        v_a1 = v_base
        v_b1 = [x + rng.gauss(0.0, 0.001) for x in v_base]
        n = math.sqrt(sum(x * x for x in v_b1))
        v_b1 = [x / n for x in v_b1]
        index.upsert(a1)
        index.upsert(b1)
        index.upsert_vectors(str(a1.id), ["c1"], [v_a1])
        index.upsert_vectors(str(b1.id), ["c2"], [v_b1])

        # Looser pair: cosine ~ 0.9 — orthogonal-noise mix.
        v_base2 = _normalized_random(50)
        a2 = _make_memory(body="m", vault=vault)
        b2 = _make_memory(body="n", vault=vault)
        rng2 = random.Random(60)
        noise = [rng2.gauss(0.0, 1.0) for _ in range(768)]
        proj = sum(x * y for x, y in zip(v_base2, noise, strict=True))
        noise = [n - proj * a_i for n, a_i in zip(noise, v_base2, strict=True)]
        n_norm = math.sqrt(sum(x * x for x in noise))
        noise_unit = [x / n_norm for x in noise]
        target = 0.9
        sin = math.sqrt(1.0 - target * target)
        v_b2 = [target * a_i + sin * n_i for a_i, n_i in zip(v_base2, noise_unit, strict=True)]
        nb = math.sqrt(sum(x * x for x in v_b2))
        v_b2 = [x / nb for x in v_b2]
        index.upsert(a2)
        index.upsert(b2)
        index.upsert_vectors(str(a2.id), ["c3"], [v_base2])
        index.upsert_vectors(str(b2.id), ["c4"], [v_b2])

        pairs = find_dedup_candidate_pairs(vault, index, min_cosine=0.85)
        assert len(pairs) >= 2
        # Sort order: descending cosine.
        assert pairs[0].cosine >= pairs[1].cosine
        assert pairs[0].cosine > 0.95


class TestLimit:
    def test_limit_truncates_results(self, vault: Vault, index: Index) -> None:
        # Build a fan: many near-identical vectors.
        v = _normalized_random(1)
        for _ in range(6):
            m = _make_memory(body="x", vault=vault)
            index.upsert(m)
            index.upsert_vectors(str(m.id), ["c"], [v])
        # 6 memories with identical vectors → C(6,2) = 15 pairs.
        all_pairs = find_dedup_candidate_pairs(vault, index, neighbors_per_memory=10)
        assert len(all_pairs) >= 5
        limited = find_dedup_candidate_pairs(vault, index, neighbors_per_memory=10, limit=3)
        assert len(limited) == 3


class TestMaxMemories:
    def test_max_memories_caps_outer_loop(self, vault: Vault, index: Index) -> None:
        # Build six identical-vector memories; full scan would surface
        # C(6,2) = 15 canonical pairs. ``max_memories`` caps the outer
        # loop, so only pairs anchored on the first M (sorted by id)
        # memories appear — every pair has at least one anchor in that
        # subset.
        v = _normalized_random(11)
        memories = []
        for _ in range(6):
            m = _make_memory(body="x", vault=vault)
            index.upsert(m)
            index.upsert_vectors(str(m.id), ["c"], [v])
            memories.append(m)

        full = find_dedup_candidate_pairs(vault, index, neighbors_per_memory=10)
        bounded = find_dedup_candidate_pairs(vault, index, neighbors_per_memory=10, max_memories=2)
        assert len(bounded) < len(full)
        assert len(bounded) > 0
        # Every bounded pair must include one of the first two memory
        # ids (sorted lexicographically) as anchor.
        anchor_set = sorted(str(m.id) for m in memories)[:2]
        for pair in bounded:
            assert pair.a_id in anchor_set or pair.b_id in anchor_set

    def test_max_memories_zero_returns_empty(self, vault: Vault, index: Index) -> None:
        v = _normalized_random(2)
        for _ in range(3):
            m = _make_memory(body="y", vault=vault)
            index.upsert(m)
            index.upsert_vectors(str(m.id), ["c"], [v])
        assert find_dedup_candidate_pairs(vault, index, max_memories=0) == []

    def test_max_memories_none_is_full_scan(self, vault: Vault, index: Index) -> None:
        v = _normalized_random(3)
        for _ in range(4):
            m = _make_memory(body="z", vault=vault)
            index.upsert(m)
            index.upsert_vectors(str(m.id), ["c"], [v])
        full_default = find_dedup_candidate_pairs(vault, index, neighbors_per_memory=10)
        full_explicit = find_dedup_candidate_pairs(
            vault, index, neighbors_per_memory=10, max_memories=None
        )
        assert {(p.a_id, p.b_id) for p in full_default} == {(p.a_id, p.b_id) for p in full_explicit}


class TestNoMutation:
    def test_no_writes_to_vault(self, vault: Vault, index: Index) -> None:
        a = _make_memory(body="alpha", vault=vault)
        b = _make_memory(body="alpha", vault=vault)
        index.upsert(a)
        index.upsert(b)
        v = _normalized_random(1)
        index.upsert_vectors(str(a.id), ["a"], [v])
        index.upsert_vectors(str(b.id), ["b"], [v])

        a_before = (vault.read(a.path).body, vault.read(a.path).frontmatter.importance)
        find_dedup_candidate_pairs(vault, index)
        a_after = (vault.read(a.path).body, vault.read(a.path).frontmatter.importance)
        assert a_before == a_after


class TestDataclassShape:
    def test_dataclass_is_frozen(self) -> None:
        pair = DedupCandidatePair(
            a_id="a",
            b_id="b",
            cosine=0.9,
            a_title=None,
            b_title=None,
            a_path="memories/a.md",
            b_path="memories/b.md",
            a_type="memory",
            b_type="memory",
        )
        # `frozen=True` raises FrozenInstanceError on attempted mutation.
        # Catch the specific dataclass exception so the test doesn't
        # silently pass on a different error type.
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            pair.cosine = 0.0  # type: ignore[misc]


class TestDefaults:
    def test_default_min_cosine_matches_adr(self) -> None:
        # ADR 0012 specifies cosine ≥ 0.85 as the default candidate
        # threshold. Don't change without intent.
        assert DEFAULT_MIN_COSINE == 0.85


class TestCli:
    def _vault_with_meta(self, tmp_path: Path) -> Path:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "_meta" / "config.yaml").write_text(f"vault_path: {root}\n", encoding="utf-8")
        return root

    def test_no_pairs_message(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "dedup-candidates", "--vault", str(root)])
        assert result.exit_code == 0, result.stdout
        assert "no pairs" in result.stdout

    def test_lists_pairs(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        idx = Index(root / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            a = _make_memory(body="alpha", vault=v, title="dup-a")
            b = _make_memory(body="alpha", vault=v, title="dup-b")
            idx.upsert(a)
            idx.upsert(b)
            vec = _normalized_random(11)
            idx.upsert_vectors(str(a.id), ["a"], [vec])
            idx.upsert_vectors(str(b.id), ["b"], [vec])
        finally:
            idx.close()

        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "dedup-candidates", "--vault", str(root)])
        assert result.exit_code == 0, result.stdout
        assert "1 candidate pair" in result.stdout
        assert "dup-a" in result.stdout
        assert "dup-b" in result.stdout

    def test_min_cosine_flag(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        idx = Index(root / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            a = _make_memory(body="alpha", vault=v)
            b = _make_memory(body="alpha", vault=v)
            idx.upsert(a)
            idx.upsert(b)
            vec = _normalized_random(11)
            idx.upsert_vectors(str(a.id), ["a"], [vec])
            idx.upsert_vectors(str(b.id), ["b"], [vec])
        finally:
            idx.close()

        runner = CliRunner()
        result_loose = runner.invoke(
            app,
            [
                "hygiene",
                "dedup-candidates",
                "--vault",
                str(root),
                "--min-cosine",
                "0.5",
            ],
        )
        assert "1 candidate pair" in result_loose.stdout

        result_too_strict = runner.invoke(
            app,
            [
                "hygiene",
                "dedup-candidates",
                "--vault",
                str(root),
                "--min-cosine",
                "1.01",
            ],
        )
        assert "no pairs" in result_too_strict.stdout
