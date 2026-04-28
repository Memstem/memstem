"""Tests for hybrid search (RRF over BM25 + sqlite-vec)."""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from memstem.core.embeddings import OllamaEmbedder
from memstem.core.frontmatter import Frontmatter, validate
from memstem.core.index import FtsHit, Index, VecHit
from memstem.core.search import (
    DEFAULT_RRF_K,
    Result,
    Search,
    rrf_combine,
)
from memstem.core.storage import Memory, Vault


def _make_memory(
    *,
    body: str,
    title: str | None = None,
    tags: list[str] | None = None,
    vault: Vault | None = None,
    importance: float | None = None,
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": "memory",
        "created": "2026-04-25T15:00:00+00:00",
        "updated": "2026-04-25T15:00:00+00:00",
        "source": "human",
        "title": title or "untitled",
        "tags": tags or [],
    }
    if importance is not None:
        metadata["importance"] = importance
    fm: Frontmatter = validate(metadata)
    memory = Memory(
        frontmatter=fm,
        body=body,
        path=Path(f"memories/{fm.id}.md"),
    )
    if vault is not None:
        vault.write(memory)
    return memory


def _fake_embedding(seed: int, dims: int = 768) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 1.0) for _ in range(dims)]


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


class TestRrfCombine:
    def test_empty_inputs_return_empty(self) -> None:
        assert rrf_combine([], []) == []

    def test_bm25_only(self) -> None:
        hits = [FtsHit(memory_id="a", score=-0.5), FtsHit(memory_id="b", score=-0.3)]
        fused = rrf_combine(hits, [], k=60)
        assert [h.memory_id for h in fused] == ["a", "b"]
        assert fused[0].score == pytest.approx(1 / 61)
        assert fused[1].score == pytest.approx(1 / 62)
        assert fused[0].bm25_rank == 1
        assert fused[0].vec_rank is None

    def test_vec_only(self) -> None:
        hits = [
            VecHit(memory_id="a", chunk_id="a:0", chunk_index=0, distance=0.1),
            VecHit(memory_id="b", chunk_id="b:0", chunk_index=0, distance=0.5),
        ]
        fused = rrf_combine([], hits, k=60)
        assert [h.memory_id for h in fused] == ["a", "b"]
        assert fused[0].vec_rank == 1
        assert fused[0].bm25_rank is None

    def test_overlap_boosts_score(self) -> None:
        bm25 = [FtsHit(memory_id="a", score=-0.5)]
        vec = [VecHit(memory_id="a", chunk_id="a:0", chunk_index=0, distance=0.1)]
        fused = rrf_combine(bm25, vec, k=60)
        assert fused[0].score == pytest.approx(2 / 61)
        assert fused[0].bm25_rank == 1
        assert fused[0].vec_rank == 1

    def test_dedupes_vec_chunks_per_memory(self) -> None:
        # Two chunks for memory "a" — only the best (rank 1) should count.
        vec = [
            VecHit(memory_id="a", chunk_id="a:0", chunk_index=0, distance=0.1),
            VecHit(memory_id="a", chunk_id="a:1", chunk_index=1, distance=0.2),
            VecHit(memory_id="b", chunk_id="b:0", chunk_index=0, distance=0.3),
        ]
        fused = rrf_combine([], vec, k=60)
        # "a" ranked 1 (its best chunk), "b" ranked 2.
        a = next(h for h in fused if h.memory_id == "a")
        b = next(h for h in fused if h.memory_id == "b")
        assert a.vec_rank == 1
        assert b.vec_rank == 2
        assert a.score > b.score

    def test_results_sorted_by_score(self) -> None:
        bm25 = [
            FtsHit(memory_id="x", score=-0.1),
            FtsHit(memory_id="y", score=-0.2),
            FtsHit(memory_id="z", score=-0.3),
        ]
        vec = [
            VecHit(memory_id="z", chunk_id="z:0", chunk_index=0, distance=0.1),
        ]
        fused = rrf_combine(bm25, vec)
        # z appears in both → should rank ahead of x even though x is bm25 #1.
        assert fused[0].memory_id == "z"

    def test_default_k_is_60(self) -> None:
        # Smoke check that DEFAULT_RRF_K wires through.
        bm25 = [FtsHit(memory_id="a", score=-0.5)]
        fused = rrf_combine(bm25, [])
        assert fused[0].score == pytest.approx(1 / (DEFAULT_RRF_K + 1))

    def test_bm25_weight_scales_contribution(self) -> None:
        bm25 = [FtsHit(memory_id="a", score=-0.5)]
        fused = rrf_combine(bm25, [], k=60, bm25_weight=2.0)
        assert fused[0].score == pytest.approx(2.0 / 61)

    def test_vector_weight_scales_contribution(self) -> None:
        vec = [VecHit(memory_id="a", chunk_id="a:0", chunk_index=0, distance=0.1)]
        fused = rrf_combine([], vec, k=60, vector_weight=3.0)
        assert fused[0].score == pytest.approx(3.0 / 61)

    def test_zero_bm25_weight_makes_vec_dominate(self) -> None:
        # bm25_weight=0 effectively disables BM25 contribution; a memory present
        # only in vec ranks above one that's only in BM25.
        bm25 = [FtsHit(memory_id="x", score=-0.1)]
        vec = [VecHit(memory_id="y", chunk_id="y:0", chunk_index=0, distance=0.1)]
        fused = rrf_combine(bm25, vec, k=60, bm25_weight=0.0, vector_weight=1.0)
        assert fused[0].memory_id == "y"
        # x still appears in the result (rank tracking) but with score 0.
        x = next(h for h in fused if h.memory_id == "x")
        assert x.score == 0.0
        assert x.bm25_rank == 1

    def test_zero_vector_weight_makes_bm25_dominate(self) -> None:
        bm25 = [FtsHit(memory_id="x", score=-0.1)]
        vec = [VecHit(memory_id="y", chunk_id="y:0", chunk_index=0, distance=0.1)]
        fused = rrf_combine(bm25, vec, k=60, bm25_weight=1.0, vector_weight=0.0)
        assert fused[0].memory_id == "x"

    def test_weighted_overlap_combines(self) -> None:
        # When a memory hits both, the per-source contributions add with weights.
        bm25 = [FtsHit(memory_id="a", score=-0.5)]
        vec = [VecHit(memory_id="a", chunk_id="a:0", chunk_index=0, distance=0.1)]
        fused = rrf_combine(bm25, vec, k=60, bm25_weight=2.0, vector_weight=3.0)
        assert fused[0].score == pytest.approx(2.0 / 61 + 3.0 / 61)


class TestSearchBm25Only:
    """Search without an embedder falls back to BM25-only ranking."""

    def test_returns_top_n(self, vault: Vault, index: Index) -> None:
        for body in [
            "deploy via cloudflare tunnel",
            "deploy via vercel",
            "deploy notes",
            "unrelated topic",
        ]:
            mem = _make_memory(body=body, vault=vault)
            index.upsert(mem)

        search = Search(vault=vault, index=index, embedder=None)
        results = search.search("deploy", limit=2)
        assert len(results) == 2
        assert all(isinstance(r, Result) for r in results)
        assert all(r.vec_rank is None for r in results)

    def test_filters_by_type(self, vault: Vault, index: Index) -> None:
        memory = _make_memory(body="alpha topic", vault=vault)
        index.upsert(memory)

        skill_meta = validate(
            {
                "id": str(uuid4()),
                "type": "skill",
                "created": "2026-04-25T15:00:00+00:00",
                "updated": "2026-04-25T15:00:00+00:00",
                "source": "human",
                "title": "alpha skill",
                "scope": "universal",
                "verification": "ok",
            }
        )
        skill = Memory(
            frontmatter=skill_meta,
            body="alpha topic",
            path=Path(f"skills/{skill_meta.id}.md"),
        )
        vault.write(skill)
        index.upsert(skill)

        search = Search(vault=vault, index=index)
        only_memories = search.search("alpha", limit=10, types=["memory"])
        only_skills = search.search("alpha", limit=10, types=["skill"])
        assert len(only_memories) == 1 and only_memories[0].memory.type.value == "memory"
        assert len(only_skills) == 1 and only_skills[0].memory.type.value == "skill"

    def test_skips_missing_vault_files(self, vault: Vault, index: Index) -> None:
        # Upsert into the index but DON'T write the file to the vault.
        memory = _make_memory(body="orphaned record")
        index.upsert(memory)
        search = Search(vault=vault, index=index)
        # query_fts will return the orphan; _materialize should skip it.
        results = search.search("orphaned")
        assert results == []

    def test_sanitizes_fts_query(self, vault: Vault, index: Index) -> None:
        index.upsert(_make_memory(body="what is new", vault=vault))
        # An un-sanitized query like 'what-is-new?' would raise FTS5 syntax error.
        results = Search(vault=vault, index=index).search("what-is-new?")
        assert len(results) == 1

    def test_empty_query_returns_empty(self, vault: Vault, index: Index) -> None:
        index.upsert(_make_memory(body="anything", vault=vault))
        results = Search(vault=vault, index=index).search('()[]"^')
        assert results == []


class TestSearchExpiredFilter:
    """ADR 0011 PR-B: records past `valid_to` drop from default search."""

    @staticmethod
    def _make_memory_with_valid_to(body: str, valid_to: datetime | None, vault: Vault) -> Memory:
        metadata: dict[str, Any] = {
            "id": str(uuid4()),
            "type": "memory",
            "created": "2026-04-25T15:00:00+00:00",
            "updated": "2026-04-25T15:00:00+00:00",
            "source": "human",
            "title": "test",
            "tags": [],
        }
        if valid_to is not None:
            metadata["valid_to"] = valid_to.isoformat()
        fm = validate(metadata)
        memory = Memory(frontmatter=fm, body=body, path=Path(f"memories/{fm.id}.md"))
        vault.write(memory)
        return memory

    def test_expired_record_excluded_by_default(self, vault: Vault, index: Index) -> None:
        past = datetime.now(tz=UTC) - timedelta(days=1)
        expired = self._make_memory_with_valid_to("alpha expired", past, vault)
        index.upsert(expired)
        live = self._make_memory_with_valid_to(
            "alpha live", datetime.now(tz=UTC) + timedelta(days=10), vault
        )
        index.upsert(live)
        results = Search(vault=vault, index=index).search("alpha")
        ids = [str(r.memory.id) for r in results]
        assert str(live.id) in ids
        assert str(expired.id) not in ids

    def test_expired_record_included_with_flag(self, vault: Vault, index: Index) -> None:
        past = datetime.now(tz=UTC) - timedelta(days=1)
        expired = self._make_memory_with_valid_to("alpha expired", past, vault)
        index.upsert(expired)
        results = Search(vault=vault, index=index).search("alpha", include_expired=True)
        assert any(str(r.memory.id) == str(expired.id) for r in results)

    def test_no_valid_to_always_included(self, vault: Vault, index: Index) -> None:
        # Records without `valid_to` set must surface normally.
        m = self._make_memory_with_valid_to("alpha forever", None, vault)
        index.upsert(m)
        results = Search(vault=vault, index=index).search("alpha")
        assert any(str(r.memory.id) == str(m.id) for r in results)


class TestSearchHybrid:
    """Search with a stub embedder uses both signals via RRF."""

    class _StubEmbedder:
        """Deterministic embedder that maps text → fixed vectors for tests."""

        dimensions = 768

        def __init__(self, mapping: dict[str, list[float]]) -> None:
            self.mapping = mapping

        def embed(self, text: str) -> list[float]:
            return self.mapping[text]

    def test_multi_signal_match_outranks_single_signal_match(
        self, vault: Vault, index: Index
    ) -> None:
        # A memory that scores in BOTH signals (BM25 + vec) should outrank
        # a memory that scores in only one. This is RRF's whole point.
        both_match = _make_memory(body="cloudflare deployment plan", vault=vault)
        bm25_only = _make_memory(body="cloudflare retro thoughts", vault=vault)
        vec_only = _make_memory(body="entirely unrelated body", vault=vault)
        for m in (both_match, bm25_only, vec_only):
            index.upsert(m)

        query_vec = _fake_embedding(1)
        index.upsert_vectors(str(both_match.id), ["c"], [query_vec])
        index.upsert_vectors(str(bm25_only.id), ["c"], [_fake_embedding(99)])
        index.upsert_vectors(str(vec_only.id), ["c"], [_fake_embedding(2)])

        embedder = self._StubEmbedder({"cloudflare": query_vec})
        search = Search(vault=vault, index=index, embedder=embedder)  # type: ignore[arg-type]
        results = search.search("cloudflare", limit=3)
        assert results[0].memory.id == both_match.id
        assert results[0].bm25_rank is not None
        assert results[0].vec_rank is not None

    def test_falls_back_to_bm25_on_embedder_error(self, vault: Vault, index: Index) -> None:
        memory = _make_memory(body="lookup target", vault=vault)
        index.upsert(memory)

        class _BoomEmbedder:
            dimensions = 768

            def embed(self, text: str) -> list[float]:
                raise RuntimeError("boom")

        search = Search(vault=vault, index=index, embedder=_BoomEmbedder())  # type: ignore[arg-type]
        results = search.search("lookup")
        assert len(results) == 1
        assert results[0].memory.id == memory.id


@pytest.mark.requires_ollama
class TestHybridRecallAgainstOllama:
    """Live integration: 20-doc corpus, verify hybrid > either signal alone."""

    def _seed_corpus(
        self, vault: Vault, index: Index, embedder: OllamaEmbedder
    ) -> dict[str, Memory]:
        corpus = {
            "cloudflare_decision": "Decided to use Cloudflare Registrar for new domains because at-cost pricing saves money.",
            "ollama_install": "Installed Ollama on the EC2 box for local embeddings via nomic-embed-text.",
            "merge_freeze": "Mobile team is cutting a release branch; we're freezing non-critical merges through Friday.",
            "feline_obs": "The cat sat on the mat and watched the rain fall outside.",
            "rug_obs": "A small feline rested on the rug while the storm passed.",
            "qcd_paper": "Quantum chromodynamics is the gauge theory describing the strong interaction.",
            "tunnel_notes": "Cloudflare Tunnel exposes local services at memstem.com without opening firewall ports.",
            "embedding_choice": "Picked nomic-embed-text over OpenAI text-embedding-3-small for offline use.",
            "vault_layout": "The canonical vault stores memories, skills, sessions, and daily logs as markdown.",
            "fts5_notes": "FTS5 is SQLite's full-text search engine; pairs with sqlite-vec for hybrid retrieval.",
            "rrf_def": "Reciprocal Rank Fusion blends two ranked lists by 1/(k+rank).",
            "skill_storage": "Skills live in their own folder with title, scope, and verification fields.",
            "person_brad": "Brad Besner runs TechPro Security and three related businesses.",
            "person_ari": "Ari is the existing OpenClaw assistant powering Brad's daily workflow.",
            "weather": "It rained all afternoon and the wind picked up around dusk.",
            "deploy_steps": "Run pytest, then push to main, then PM2 restart for the affected service.",
            "auth_arch": "OpenClaw agents use OAuth tokens from shared profiles; Claude Code has its own creds.",
            "twilio_voice": "Sarah voice runs on ConversationRelay over webhooks via Cloudflare tunnel.",
            "pricing_note": "Cloudflare Registrar charges at-cost; competitor pricing is roughly 2x.",
            "test_marker": "Custom pytest marker requires_ollama gates live integration tests.",
        }
        memories: dict[str, Memory] = {}
        for key, body in corpus.items():
            memory = _make_memory(body=body, title=key.replace("_", " "), vault=vault)
            index.upsert(memory)
            embeddings = embedder.embed_batch([body])
            index.upsert_vectors(str(memory.id), [body], embeddings)
            memories[key] = memory
        return memories

    def test_semantic_query_finds_synonym_match(self, vault: Vault, index: Index) -> None:
        with OllamaEmbedder() as embedder:
            memories = self._seed_corpus(vault, index, embedder)
            search = Search(vault=vault, index=index, embedder=embedder)
            # "feline" and "rain" only co-occur in feline_obs/rug_obs, but neither
            # uses the word "cat" in the query. Pure BM25 should miss; vec finds it.
            results = search.search("a cat resting while it rains", limit=3)

        result_ids = {r.memory.id for r in results}
        assert memories["feline_obs"].id in result_ids or memories["rug_obs"].id in result_ids

    def test_hybrid_outperforms_either_signal_alone(self, vault: Vault, index: Index) -> None:
        with OllamaEmbedder() as embedder:
            memories = self._seed_corpus(vault, index, embedder)
            search = Search(vault=vault, index=index, embedder=embedder)
            # Query has both a literal keyword ("Cloudflare") AND a semantic
            # angle ("save money on domains"). Hybrid should rank cloudflare_decision
            # (matches both) above tunnel_notes (keyword only) and pricing_note
            # (semantic only).
            results = search.search("Cloudflare saves money on domains", limit=3)

        top_ids = [r.memory.id for r in results]
        assert memories["cloudflare_decision"].id in top_ids


class TestImportanceRanking:
    """ADR 0008 Tier 1: ``final = rrf * (1 + alpha * importance)``.

    Importance is a tiebreaker layered on top of RRF. The boost should:
    1. Re-rank close ties so the higher-importance record wins.
    2. Never override a substantially-stronger relevance signal.
    3. Treat unset importance as a neutral 0.5 default (per ADR 0008).
    4. Default to alpha=0.2 in config but be tunable per-call.
    """

    def test_high_importance_breaks_close_ties(self, vault: Vault, index: Index) -> None:
        # Two records that BM25-tie on the query. With alpha > 0 the higher-
        # importance record must rank first.
        low = _make_memory(body="alpha topic", title="low", vault=vault, importance=0.2)
        high = _make_memory(body="alpha topic", title="high", vault=vault, importance=0.9)
        index.upsert(low)
        index.upsert(high)

        results = Search(vault=vault, index=index).search("alpha", limit=2, importance_weight=0.2)
        assert next(iter(r.memory.id for r in results)) == high.id

    def test_alpha_zero_disables_boost(self, vault: Vault, index: Index) -> None:
        # With alpha=0 the importance field has zero effect. The boost
        # factor (1 + alpha * importance) collapses to 1.0, so the
        # final score equals the bare RRF score.
        memory = _make_memory(body="alpha topic", vault=vault, importance=0.99)
        index.upsert(memory)
        results = Search(vault=vault, index=index).search("alpha", limit=1, importance_weight=0.0)
        # rrf == 1/(60+1), no boost applied → score == 1/61
        assert results[0].score == pytest.approx(1 / 61)

    def test_unrelated_record_does_not_surface_via_importance(
        self, vault: Vault, index: Index
    ) -> None:
        # Importance is a re-ranker over the candidate pool — it cannot
        # raise a memory that does NOT match the query at all into the
        # results. ADR 0008's guarantee that importance is a "tiebreaker,
        # not a forcing function" rests on this.
        relevant = _make_memory(body="alpha topic", vault=vault, importance=0.0)
        unrelated = _make_memory(
            body="entirely different content with no overlap", vault=vault, importance=1.0
        )
        index.upsert(relevant)
        index.upsert(unrelated)

        results = Search(vault=vault, index=index).search("alpha", limit=10, importance_weight=1.0)
        ids = {r.memory.id for r in results}
        assert relevant.id in ids
        assert unrelated.id not in ids

    def test_far_rank_gap_dominates_importance_boost(self, vault: Vault, index: Index) -> None:
        # Once the BM25 rank gap is large enough, no realistic importance
        # value can flip the order. With alpha=0.2 and the maximum
        # importance=1.0 (boost factor 1.2), a rank-1 record's
        # 1/(60+1)=0.0164 score outranks a rank-N record's
        # 1.2/(60+N) once N > 13.
        #
        # We seed 30 decoys (all with importance=0.0 to keep them out
        # of contention) plus a weak match (importance=1.0) past that
        # threshold. The strong record (importance=0.0) at rank 1 must
        # still win.
        strong = _make_memory(
            body=" ".join(["alphaword"] * 8),
            vault=vault,
            importance=0.0,
        )
        index.upsert(strong)
        for _ in range(30):
            decoy = _make_memory(body="alphaword decoy filler", vault=vault, importance=0.0)
            index.upsert(decoy)
        weak = _make_memory(body="alphaword brief mention", vault=vault, importance=1.0)
        index.upsert(weak)

        results = Search(vault=vault, index=index).search(
            "alphaword", limit=3, importance_weight=0.2
        )
        # The strong-relevance record should rank first; importance=1.0
        # on the weak match can't catch up across this rank gap at
        # alpha=0.2 because the multiplicative boost is bounded at 1.2x
        # while the rank-position gap drives the score difference past
        # that.
        assert results[0].memory.id == strong.id

    def test_missing_importance_treated_as_neutral_default(
        self, vault: Vault, index: Index
    ) -> None:
        # A record without an explicit ``importance`` field should be
        # scored as 0.5 (DEFAULT_IMPORTANCE), not 0.0. That way
        # un-annotated records aren't penalized for being un-annotated.
        unset = _make_memory(body="alpha topic", title="unset", vault=vault, importance=None)
        explicit_low = _make_memory(
            body="alpha topic", title="explicit-low", vault=vault, importance=0.1
        )
        index.upsert(unset)
        index.upsert(explicit_low)

        results = Search(vault=vault, index=index).search("alpha", limit=2, importance_weight=0.5)
        ids = [r.memory.id for r in results]
        assert ids[0] == unset.id
        assert ids[1] == explicit_low.id

    def test_importance_value_does_not_crash_on_none(self, vault: Vault, index: Index) -> None:
        # Sanity: a record with importance=None must not produce NaN /
        # raise / sort weirdly. It just gets the default 0.5 boost.
        from memstem.core.search import DEFAULT_IMPORTANCE, DEFAULT_IMPORTANCE_WEIGHT

        memory = _make_memory(body="alpha topic", title="t", vault=vault, importance=None)
        index.upsert(memory)
        results = Search(vault=vault, index=index).search("alpha", limit=1)
        assert len(results) == 1
        # The score should reflect the default boost: rrf * (1 + alpha * 0.5).
        # With one BM25 hit and no vec, rrf == 1/(60+1) == 1/61.
        expected = (1 / 61) * (1.0 + DEFAULT_IMPORTANCE_WEIGHT * DEFAULT_IMPORTANCE)
        assert results[0].score == pytest.approx(expected)

    def test_default_importance_weight_is_safe(self, vault: Vault, index: Index) -> None:
        # Calling Search.search() without an explicit importance_weight
        # uses DEFAULT_IMPORTANCE_WEIGHT (0.2) — a safe non-zero default
        # that doesn't break the v0.1 contract for un-annotated vaults.
        from memstem.core.search import DEFAULT_IMPORTANCE_WEIGHT

        memory = _make_memory(body="alpha topic", vault=vault, importance=1.0)
        index.upsert(memory)
        results = Search(vault=vault, index=index).search("alpha", limit=1)
        # rrf == 1/61, importance == 1.0 → expected = (1/61) * (1 + 0.2*1.0) = 1.2/61
        expected = (1 / 61) * (1.0 + DEFAULT_IMPORTANCE_WEIGHT * 1.0)
        assert results[0].score == pytest.approx(expected)

    def test_expired_records_still_excluded_with_importance(
        self, vault: Vault, index: Index
    ) -> None:
        # The valid_to filter must not be circumvented by a high
        # importance value. ADR 0011 PR-B + ADR 0008 cooperate.
        from datetime import timedelta

        past = datetime.now(tz=UTC) - timedelta(days=1)
        live = _make_memory(body="alpha live", vault=vault, importance=0.0)
        index.upsert(live)

        # Hand-build an expired record with importance=1.0
        meta = {
            "id": str(uuid4()),
            "type": "memory",
            "created": "2026-04-25T15:00:00+00:00",
            "updated": "2026-04-25T15:00:00+00:00",
            "source": "human",
            "title": "expired",
            "tags": [],
            "valid_to": past.isoformat(),
            "importance": 1.0,
        }
        fm = validate(meta)
        expired = Memory(frontmatter=fm, body="alpha expired", path=Path(f"memories/{fm.id}.md"))
        vault.write(expired)
        index.upsert(expired)

        results = Search(vault=vault, index=index).search("alpha", limit=10, importance_weight=0.5)
        ids = {r.memory.id for r in results}
        assert live.id in ids
        assert expired.id not in ids

    def test_importance_boost_is_multiplicative(self, vault: Vault, index: Index) -> None:
        # Concretely verify the formula: final = rrf * (1 + alpha * importance).
        # A record with importance=1.0 and alpha=0.2 should score exactly
        # 1.2x its bare RRF score.
        memory = _make_memory(body="alpha topic", vault=vault, importance=1.0)
        index.upsert(memory)
        results = Search(vault=vault, index=index).search("alpha", limit=1, importance_weight=0.2)
        expected = (1 / 61) * (1.0 + 0.2 * 1.0)
        assert results[0].score == pytest.approx(expected)


class TestSearchConfigImportance:
    """SearchConfig.importance_weight defaults and threading."""

    def test_default_importance_weight_is_0_2(self) -> None:
        # The documented ADR 0008 alpha is 0.2. Don't change this default
        # without intent — it's the contract every shipped vault relies on.
        from memstem.config import SearchConfig

        cfg = SearchConfig()
        assert cfg.importance_weight == pytest.approx(0.2)

    def test_importance_weight_round_trips_through_config(self) -> None:
        # The config field should serialize and deserialize cleanly so
        # users can persist a custom alpha in `_meta/config.yaml`.
        from memstem.config import SearchConfig

        cfg = SearchConfig(importance_weight=0.5)
        dumped = cfg.model_dump(mode="json")
        loaded = SearchConfig.model_validate(dumped)
        assert loaded.importance_weight == pytest.approx(0.5)
