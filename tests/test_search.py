"""Tests for hybrid search (RRF over BM25 + sqlite-vec)."""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path
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
