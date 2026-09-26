"""Tests for shadow-mode Jev reranking (ADR 0044)."""

from __future__ import annotations

import json
import random
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from memstem.config import JevShadowConfig
from memstem.core.frontmatter import validate
from memstem.core.index import Index
from memstem.core.jev_shadow import (
    UNKNOWN_COST_RESERVE_USD,
    Candidate,
    JevShadow,
    ShadowSettings,
    ShadowStore,
    build_jev_shadow,
    build_request,
    parse_scores,
    redact,
    rerank_order,
    select_passages,
    words,
)
from memstem.core.search import Search
from memstem.core.storage import Memory, Vault


def _reference_excerpt(query: str, body: str, budget: int) -> str:
    """The pilot's selector (memstem.eval.jev_trial.excerpt, passages mode)."""
    if len(body) <= budget:
        return body[:budget]
    width = max(100, budget // 3 - 25)
    chunks = [(start, body[start : start + width]) for start in range(0, len(body), width)]
    query_words = words(query)
    selected = sorted(chunks, key=lambda x: (-len(words(x[1]) & query_words), x[0]))[:3]
    return "\n[…]\n".join(part for _, part in sorted(selected))[:budget]


def _answer(score: float) -> dict[str, Any]:
    # Probabilities whose expectation equals ``score`` (0..3).
    low = int(score) if score < 3 else 2
    frac = score - low
    probs = {str(k): 0.0 for k in range(4)}
    probs[str(low)] = 1 - frac
    probs[str(low + 1)] = frac
    return {"type": "score", "score": score, "confidence": 0.9, "probabilities": probs}


def _memory(title: str, body: str) -> Memory:
    fm = validate(
        {
            "id": str(uuid4()),
            "type": "memory",
            "created": "2026-09-01T00:00:00+00:00",
            "updated": "2026-09-02T00:00:00+00:00",
            "source": "human",
            "title": title,
            "tags": [],
        }
    )
    return Memory(frontmatter=fm, body=body, path=Path(f"memories/{fm.id}.md"))


class _Hit:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory


class _Outcome:
    def __init__(self, results: list[_Hit], degraded: bool = False) -> None:
        self.results = results
        self.degraded = degraded


class TestSelectPassages:
    def test_matches_pilot_selector(self) -> None:
        rng = random.Random(7)
        vocab = [
            "memstem", "jev", "rerank", "gateway", "stop-sign", "Oaks", "pm2", "zoho",
            "quote", "template", "the", "and", "ari", "deploy", "rollback", "ß", "naïve",
        ]  # fmt: skip
        mismatches = 0
        for trial in range(300):
            body = " ".join(rng.choice(vocab) for _ in range(rng.randint(50, 3000)))
            body = body.replace(" ", rng.choice([" ", "\n", ", "]), rng.randint(0, 40))
            query = " ".join(rng.sample(vocab, 4))
            for budget in (1600, 1200, 379):
                mismatches += select_passages(query, body, budget) != _reference_excerpt(
                    query, body, budget
                )
            assert trial >= 0
        # Only words cut by a window edge may differ (fragment coincidences).
        assert mismatches <= 3

    def test_short_body_returned_whole(self) -> None:
        assert select_passages("anything", "short body", 1600) == "short body"

    def test_prefers_windows_with_query_words(self) -> None:
        filler = "lorem ipsum " * 400
        body = filler + " jev rerank shadow " + filler
        out = select_passages("jev rerank shadow", body, 600)
        assert "jev rerank shadow" in out


class TestRedaction:
    def test_common_secret_shapes(self) -> None:
        text = (
            "tsql -S host -U damon -P 'hunter22x' -D db\n"
            "password: s3cretvalue\napi_key=abcd1234efgh\n"
            "https://user:pa55word@example.com/x\nsk-or-v1-" + "a" * 30
        )
        out = redact(text)
        for secret in ("hunter22x", "s3cretvalue", "abcd1234efgh", "pa55word", "a" * 30):
            assert secret not in out

    def test_secret_straddling_window_edge_is_redacted(self) -> None:
        # Put the secret right at the edge of the only matching window.
        pad = "x " * 300
        body = pad + "jev shadow password: TOPSECRETVALUE9 " + pad * 5
        cands = [Candidate(id="m1", title="t", updated="u", body=body)]
        for budget in (1600, 900, 400, 200):
            payload, _, _ = build_request(
                "jev shadow",
                "2026-09-26",
                cands,
                model="m",
                excerpt_chars=budget,
                request_byte_limit=100_000,
                seed="s",
            )
            text = json.dumps(payload)
            assert "TOPSECRETVALUE9" not in text
            assert "OPSECRETVALUE9" not in text

    def test_titles_redacted(self) -> None:
        cands = [Candidate(id="m1", title="password=abc123456", updated="u", body="b")]
        payload, _, _ = build_request(
            "q", "now", cands, model="m", excerpt_chars=1600, request_byte_limit=30_000, seed="s"
        )
        assert "abc123456" not in json.dumps(payload)


class TestBuildRequest:
    def test_opaque_shuffled_ids_and_mapping(self) -> None:
        cands = [
            Candidate(id=f"mem-{i}", title=f"T{i}", updated="u", body=f"b{i}") for i in range(5)
        ]
        payload, mapping, budget = build_request(
            "q", "now", cands, model="m", excerpt_chars=1600, request_byte_limit=30_000, seed="s"
        )
        assert sorted(mapping) == sorted(c.id for c in cands)
        assert set(payload["state"]["documents"]) == {f"d{i}" for i in range(5)}
        assert "mem-" not in json.dumps(payload)
        for i, cid in enumerate(mapping):
            assert payload["state"]["documents"][f"d{i}"]["title"] == "T" + cid.split("-")[1]
        assert budget == 1600

    def test_shrinks_budget_to_fit(self) -> None:
        cands = [
            Candidate(id=str(i), title="t", updated="u", body="jev " * 5000) for i in range(20)
        ]
        payload, _, budget = build_request(
            "jev", "now", cands, model="m", excerpt_chars=1600, request_byte_limit=30_000, seed="s"
        )
        assert budget < 1600
        assert len(json.dumps(payload).encode()) <= 30_000

    def test_impossible_limit_raises(self) -> None:
        cands = [Candidate(id="1", title="t" * 5000, updated="u", body="b")]
        with pytest.raises(ValueError):
            build_request(
                "q", "now", cands, model="m", excerpt_chars=1600, request_byte_limit=1_000, seed="s"
            )


class TestParseScores:
    def test_valid(self) -> None:
        response = {"answers": {"d0": _answer(3.0), "d1": _answer(1.5)}}
        assert parse_scores(response, ["a", "b"]) == {"a": 1.0, "b": 0.5}

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda r: r["answers"].pop("d1"),
            lambda r: r["answers"]["d0"].update(score=True),
            lambda r: r["answers"]["d0"].update(score=2.5),
            lambda r: r["answers"]["d0"].update(type="classify"),
            lambda r: r["answers"]["d0"]["probabilities"].pop("3"),
            lambda r: r["answers"]["d0"].update(confidence=1.5),
        ],
    )
    def test_invalid(self, mutate: Any) -> None:
        response = {"answers": {"d0": _answer(3.0), "d1": _answer(1.5)}}
        mutate(response)
        with pytest.raises(ValueError):
            parse_scores(response, ["a", "b"])

    def test_rerank_order_ties_keep_pool_order(self) -> None:
        assert rerank_order(["a", "b", "c"], {"a": 0.5, "b": 1.0, "c": 0.5}) == ["b", "a", "c"]


def _transport(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _shadow(tmp_path: Path, handler: Any, **settings: Any) -> JevShadow:
    return JevShadow(
        ShadowSettings(**settings),
        api_key="test-key",
        store=ShadowStore(tmp_path / "_meta" / "jev-shadow.db"),
        client=_transport(handler),
    )


def _ok_handler(seen: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append({"payload": payload, "auth": request.headers["authorization"]})
        docs = payload["state"]["documents"]
        answers = {
            key: _answer(3.0 if "winner" in doc["body"] else 0.0) for key, doc in docs.items()
        }
        return httpx.Response(
            200,
            json={"answers": answers, "model": "typesafe/jev-1.13-test", "usage": {"cost": 0.0004}},
        )

    return handler


def _rows(shadow: JevShadow) -> list[dict[str, Any]]:
    db = sqlite3.connect(shadow.store.path)
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute("SELECT * FROM shadow_runs ORDER BY id")]


class TestJevShadowRun:
    def _job_args(self) -> dict[str, Any]:
        served = [_Hit(_memory(f"served {i}", f"plain body {i}")) for i in range(3)]
        winner = _Hit(_memory("pool winner", "this is the winner body"))
        pool = _Outcome([served[1], winner, served[0]])
        return {"served": served, "winner": winner, "pool": pool}

    def test_success_records_would_be_order(self, tmp_path: Path) -> None:
        seen: list[dict[str, Any]] = []
        shadow = _shadow(tmp_path, _ok_handler(seen))
        a = self._job_args()
        assert shadow.submit(
            client="mcp",
            query="which is the winner",
            limit=3,
            types=None,
            served=a["served"],
            degraded=False,
            run_pool=lambda: a["pool"],
        )
        shadow.drain()
        (row,) = _rows(shadow)
        assert row["status"] == "ok"
        pool_ids = json.loads(row["pool_ids"])
        # Pilot pool shape: wider search first, then served hits it missed.
        assert pool_ids[:3] == [str(h.memory.id) for h in a["pool"].results]
        assert pool_ids[3] == str(a["served"][2].memory.id)
        assert json.loads(row["jev_order"])[0] == str(a["winner"].memory.id)
        assert json.loads(row["served_ids"]) == [str(h.memory.id) for h in a["served"]]
        assert row["cost_usd"] == pytest.approx(0.0004)
        assert row["model"] == "typesafe/jev-1.13-test"
        assert seen[0]["auth"] == "Bearer test-key"
        assert row["prep_ms"] is not None and row["api_ms"] is not None
        assert stat.S_IMODE(shadow.store.path.stat().st_mode) == 0o600

    def test_timeout_records_error_and_reserve(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        shadow = _shadow(tmp_path, handler)
        a = self._job_args()
        shadow.submit(
            client="http", query="q words", limit=5, types=None,
            served=a["served"], degraded=False, run_pool=lambda: a["pool"],
        )  # fmt: skip
        shadow.drain()
        (row,) = _rows(shadow)
        assert row["status"] == "error" and "ReadTimeout" in row["error"]
        assert row["cost_usd"] == UNKNOWN_COST_RESERVE_USD and row["cost_known"] == 0
        assert row["jev_order"] is None

    def test_invalid_response_is_error(self, tmp_path: Path) -> None:
        shadow = _shadow(tmp_path, lambda r: httpx.Response(200, json={"answers": {}}))
        a = self._job_args()
        row = shadow.run(_job(a))
        assert row["status"] == "error" and "ValueError" in row["error"]

    def test_budget_exhausted_skips_call(self, tmp_path: Path) -> None:
        seen: list[dict[str, Any]] = []
        shadow = _shadow(tmp_path, _ok_handler(seen), daily_budget_usd=0.001)
        row = shadow.run(_job(self._job_args()))
        assert row["status"] == "budget_skipped" and not seen

    def test_pool_search_failure_recorded(self, tmp_path: Path) -> None:
        shadow = _shadow(tmp_path, _ok_handler([]))

        def boom() -> Any:
            raise RuntimeError("index busy")

        row = shadow.run(_job(self._job_args(), run_pool=boom))
        assert row["status"] == "error" and "index busy" in row["error"]

    @pytest.mark.parametrize(
        ("query", "limit", "types"),
        [
            ("memstem watchdog liveness probe", 1, ["__watchdog__"]),
            ("normal question here", 10, ["__watchdog__"]),
            ("normal question here", 2, None),
            ("what is the MOM password", 10, None),
            ("   ", 10, None),
        ],
    )
    def test_filters(self, tmp_path: Path, query: str, limit: int, types: Any) -> None:
        shadow = _shadow(tmp_path, _ok_handler([]))
        a = self._job_args()
        assert not shadow.submit(
            client="mcp", query=query, limit=limit, types=types,
            served=a["served"], degraded=False, run_pool=lambda: a["pool"],
        )  # fmt: skip

    def test_full_queue_drops_without_blocking(self, tmp_path: Path) -> None:
        shadow = _shadow(tmp_path, _ok_handler([]), queue_size=1)
        a = self._job_args()
        shadow._worker = _AliveThread()  # type: ignore[assignment]  # worker never drains
        kwargs: dict[str, Any] = {
            "client": "mcp",
            "query": "some query",
            "limit": 5,
            "types": None,
            "served": a["served"],
            "degraded": False,
            "run_pool": lambda: a["pool"],
        }
        assert shadow.submit(**kwargs)
        assert not shadow.submit(**kwargs)
        assert shadow.dropped == 1


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def _job(a: dict[str, Any], run_pool: Any = None) -> Any:
    from memstem.core.jev_shadow import _Job

    return _Job(
        "mcp", "which is the winner", 3, a["served"], False, run_pool or (lambda: a["pool"])
    )


class TestBuildJevShadow:
    def test_disabled_returns_none(self, tmp_path: Path) -> None:
        assert build_jev_shadow(JevShadowConfig(), tmp_path) is None

    def test_missing_key_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMSTEM_TEST_NO_KEY", "")
        monkeypatch.setattr("memstem.auth.get_secret", lambda *a, **k: None)
        cfg = JevShadowConfig(enabled=True, api_key_env="MEMSTEM_TEST_NO_KEY")
        assert build_jev_shadow(cfg, tmp_path) is None

    def test_enabled_with_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMSTEM_TEST_JEV_KEY", "k")
        cfg = JevShadowConfig(enabled=True, api_key_env="MEMSTEM_TEST_JEV_KEY", pool_size=25)
        shadow = build_jev_shadow(cfg, tmp_path)
        assert shadow is not None and shadow.settings.pool_size == 25
        assert (tmp_path / "_meta" / "jev-shadow.db").exists()

    def test_unwritable_ledger_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMSTEM_TEST_JEV_KEY", "k")
        blocker = tmp_path / "file"
        blocker.write_text("not a dir")
        cfg = JevShadowConfig(enabled=True, api_key_env="MEMSTEM_TEST_JEV_KEY")
        assert build_jev_shadow(cfg, blocker) is None


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


class TestSearchIntegration:
    def _seed(self, vault: Vault, index: Index) -> None:
        for i in range(30):
            memory = _memory(
                f"note {i}", f"shadow rerank note {i} " + ("winner " if i == 25 else "")
            )
            vault.write(memory)
            index.upsert(memory)

    def test_served_results_unchanged_and_job_logged(
        self, vault: Vault, index: Index, tmp_path: Path
    ) -> None:
        self._seed(vault, index)
        plain = Search(vault=vault, index=index).search_with_status("shadow rerank", limit=5)
        shadow = _shadow(tmp_path, _ok_handler([]))
        search = Search(vault=vault, index=index, shadow=shadow)
        served = search.search_with_status("shadow rerank", limit=5, shadow_client="mcp")
        assert [r.memory.id for r in served.results] == [r.memory.id for r in plain.results]
        shadow.drain()
        (row,) = _rows(shadow)
        assert row["status"] == "ok" and row["client"] == "mcp"
        assert len(json.loads(row["pool_ids"])) == 20
        assert row["n_candidates"] == 20

    def test_no_shadow_client_no_job(self, vault: Vault, index: Index, tmp_path: Path) -> None:
        self._seed(vault, index)
        shadow = _shadow(tmp_path, _ok_handler([]))
        search = Search(vault=vault, index=index, shadow=shadow)
        search.search_with_status("shadow rerank", limit=5)
        shadow.drain()
        assert _rows(shadow) == []

    def test_shadow_error_never_reaches_caller(
        self, vault: Vault, index: Index, tmp_path: Path
    ) -> None:
        self._seed(vault, index)

        class Exploding(JevShadow):
            def submit(self, **kwargs: Any) -> bool:
                raise RuntimeError("shadow bug")

        shadow = Exploding(
            ShadowSettings(), api_key="k", store=ShadowStore(tmp_path / "_meta" / "s.db")
        )
        plain = Search(vault=vault, index=index).search_with_status("shadow rerank", limit=5)
        search = Search(vault=vault, index=index, shadow=shadow)
        out = search.search_with_status("shadow rerank", limit=5, shadow_client="http")
        assert [r.memory.id for r in out.results] == [r.memory.id for r in plain.results]
