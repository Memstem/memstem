"""Exercise dataset preparation without live network or private data."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from memstem.eval.jev_trial import digest


@pytest.fixture
def cli() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/jev_trial.py"
    spec = importlib.util.spec_from_file_location("trial_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sampler_deduplicates_results_and_is_read_only(cli: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE query_log (query TEXT, ts TEXT, kind TEXT)")
        db.executemany(
            "INSERT INTO query_log VALUES (?, '2026-09-26', ?)",
            [("a question", "search")] * 10 + [("b question", "search"), ("ignored get", "get")],
        )
    before = path.read_bytes()
    result = cli.sample_queries(path, 100, 123)
    assert len(result["queries"]) == 2
    assert path.read_bytes() == before
    assert cli.sample_queries(path, 100, 123) == result


def test_blind_packet_omits_rank_and_labels(cli: ModuleType) -> None:
    dataset = json.loads((Path(__file__).parents[1] / "eval/jev/fixtures.json").read_text())
    packet = cli.blind_packet(dataset)
    assert packet["dataset_sha256"] == digest(dataset)
    for c in packet["cases"]:
        assert "baseline" not in c
        assert "split" not in c
        assert all(d["grade"] is None for d in c["documents"])


def test_label_packet_must_match_original(cli: ModuleType) -> None:
    dataset = json.loads((Path(__file__).parents[1] / "eval/jev/fixtures.json").read_text())
    packet = cli.blind_packet(dataset)
    dataset["cases"][0]["query"] += "edited"
    with pytest.raises(ValueError, match="different frozen"):
        cli.apply_labels(dataset, packet)


def test_capture_rejects_unknown_groups_before_network(cli: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="grouping"):
        cli.capture(
            {"queries": [{"split": "TODO", "category": "TODO", "group": "TODO"}]},
            tmp_path,
            "http://127.0.0.1:7821",
            20,
        )


def test_capture_does_not_send_zero_rerank_override(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta/config.yaml").write_text("search:\n  reranker:\n    enabled: false\n")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"vault_path": str(tmp_path)})
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    manifest = {
        "queries": [
            {
                "id": "q",
                "query": "what happened",
                "group": "one-event",
                "split": "dev",
                "category": "no_answer",
                "origin": "real",
            }
        ]
    }
    dataset = cli.capture(manifest, tmp_path, "http://127.0.0.1:7821", 20)
    assert len(requests) == 3
    assert all("rerank_top_n" not in json.loads(r.content) for r in requests[1:])
    assert dataset["capture"]["reranker_enabled"] is False


def test_capture_refuses_active_reranker(cli: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta/config.yaml").write_text("search:\n  reranker:\n    enabled: true\n")
    with pytest.raises(ValueError, match="already-disabled"):
        cli.capture({"queries": []}, tmp_path, "http://127.0.0.1:7821", 20)


def test_capture_refuses_wrong_vault(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta/config.yaml").write_text("search: {}\n")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"vault_path": "/different/tenant"})
        )
    )
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(ValueError, match="selected vault"):
        cli.capture({"queries": []}, tmp_path, "http://127.0.0.1:7821", 20)
