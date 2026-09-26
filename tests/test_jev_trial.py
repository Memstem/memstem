from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from memstem.eval.jev_trial import (
    PROTOCOL,
    Json,
    check_lock,
    digest,
    evaluate_order,
    excerpt,
    lock_dataset,
    make_request,
    paired_bootstrap,
    parse_scores,
    run_trial,
    summarize,
    validate,
)


@pytest.fixture
def dataset() -> Json:
    return json.loads((Path(__file__).parents[1] / "eval/jev/fixtures.json").read_text())


def reply(payload: Json) -> Json:
    return {
        "model": "typesafe/jev-1.13-pinned",
        "answers": {
            key: {
                "type": "score",
                "score": 1.0,
                "confidence": 0.9,
                "probabilities": {"0": 0, "1": 1, "2": 0, "3": 0},
            }
            for key in payload["questions"]
        },
        "usage": {"cost": 0.0001},
    }


def test_labels_and_hashes(dataset: Json) -> None:
    validate(dataset)
    dataset["cases"][0]["candidates"][0]["body"] += "changed"
    with pytest.raises(ValueError, match="body changed"):
        validate(dataset)


def test_blind_evidence_must_be_actual_quote(dataset: Json) -> None:
    dataset["cases"][0]["labels"]["b"]["evidence"] = "invented answer"
    with pytest.raises(ValueError, match="Evidence is not"):
        validate(dataset)


def test_no_keyword_matching_shortcut(dataset: Json) -> None:
    del dataset["cases"][0]["labels"]["a"]
    with pytest.raises(ValueError, match="explicit label"):
        validate(dataset)


def test_leakage_between_related_queries(dataset: Json) -> None:
    dataset["cases"][10]["split"] = "test"
    with pytest.raises(ValueError, match="leaks"):
        validate(dataset)


def test_model_never_sees_gold_labels_or_baseline(dataset: Json) -> None:
    case = dataset["cases"][0]
    case["labels"]["b"]["rationale"] = "SECRET_GOLD_RATIONALE"
    case["reviewer"] = "SECRET_REVIEWER"
    payload, mapping = make_request(case, "head", PROTOCOL, 0)
    serialized = json.dumps(payload)
    assert "SECRET" not in serialized
    assert "baseline" not in serialized
    assert "body_sha256" not in serialized
    assert set(mapping) == {"a", "b", "c", "d"}
    assert len(serialized.encode()) <= PROTOCOL["request_byte_limit"]


def test_passages_find_answer_beyond_prefix() -> None:
    body = "routine unrelated chatter. " * 400 + "Approved rollback command: restore release-42."
    assert "release-42" not in excerpt("approved rollback command", body, "head", 800)
    assert "release-42" in excerpt("approved rollback command", body, "passages", 800)


def test_hit_and_recall_are_different() -> None:
    labels = {"a": {"grade": 3, "harmful": False}, "b": {"grade": 3, "harmful": False}}
    m = evaluate_order(["a"], labels)
    assert m["hit10"] == 1
    assert m["recall10"] == 0.5
    assert 0 < m["ndcg10"] < 1


def test_unanswerable_not_in_accuracy_denominator() -> None:
    metrics = evaluate_order(["a"], {"a": {"grade": 0, "harmful": True}})
    assert metrics["ndcg10"] is None
    assert metrics["mrr10"] is None
    assert metrics["harmful1"] == 1


def test_duplicate_rank_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid ranking"):
        evaluate_order(["a", "a"], {"a": {"grade": 3}})


def test_correct_ndcg_and_mrr() -> None:
    labels = {"a": {"grade": 0, "harmful": False}, "b": {"grade": 3, "harmful": False}}
    metrics = evaluate_order(["a", "b"], labels)
    assert metrics["mrr10"] == 0.5
    assert metrics["ndcg10"] == pytest.approx(0.63092975357)


def test_cluster_bootstrap_does_not_count_repeats_as_independent() -> None:
    rows = [{"group": "same", "baseline": {"mrr10": 0}, "candidate": {"mrr10": 1}}] * 20
    stats = paired_bootstrap(rows, "mrr10", 100, 1)
    assert stats == {"delta": 1, "low": 1, "high": 1, "groups": 1}


@pytest.mark.parametrize("bad", [None, "1", True, float("nan"), float("inf"), -1, 4])
def test_invalid_provider_score_rejected(bad: object) -> None:
    response = reply({"questions": {"d0": {}}})
    response["answers"]["d0"]["score"] = bad
    with pytest.raises(ValueError):
        parse_scores(response, ["id"])


def test_partial_and_fabricated_candidate_ids_rejected() -> None:
    with pytest.raises(ValueError, match="Missing or extra"):
        parse_scores(reply({"questions": {"d0": {}}}), ["a", "b"])


def test_probability_consistency() -> None:
    response = reply({"questions": {"d0": {}}})
    response["answers"]["d0"]["score"] = 3
    with pytest.raises(ValueError, match="disagrees"):
        parse_scores(response, ["a"])


def test_protocol_and_data_lock(dataset: Json) -> None:
    lock = lock_dataset(dataset, copy.deepcopy(PROTOCOL))
    lock["protocol"]["confidence_floor"] = 0.8
    with pytest.raises(ValueError, match="changed after lock"):
        check_lock(dataset, lock)


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "server", "malformed", "missing_scores"])
def test_failure_preserves_exact_baseline(dataset: Json, tmp_path: Path, mode: str) -> None:
    dataset["cases"] = dataset["cases"][:1]

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("test", request=request)
        if mode in {"rate_limit", "server"}:
            return httpx.Response(429 if mode == "rate_limit" else 503)
        if mode == "malformed":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(200, json={"answers": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_trial(
            dataset,
            lock_dataset(dataset, PROTOCOL),
            tmp_path / "run",
            api_key="fake-secret",
            split="dev",
            budget_usd=0.1,
            repeats=1,
            client=client,
        )
    assert len(result["rows"]) == 2
    for row in result["rows"]:
        assert row["fallback"]
        assert row["order"] == dataset["cases"][0]["baseline"]
        assert row["cost_usd"] > 0
        assert not row["cost_known"]
    assert "fake-secret" not in (tmp_path / "run/attempts.jsonl").read_text()


def test_budget_rejected_before_any_request(dataset: Json, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds budget"):
        run_trial(
            dataset,
            lock_dataset(dataset, PROTOCOL),
            tmp_path / "run",
            api_key="fake",
            split="dev",
            budget_usd=0.00001,
            repeats=1,
        )
    assert not (tmp_path / "run").exists()


def test_smoke_cannot_be_a_rollout_pass(dataset: Json, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply(json.loads(request.content)))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        lock = lock_dataset(dataset, PROTOCOL)
        result = run_trial(
            dataset,
            lock,
            tmp_path / "run",
            api_key="fake",
            split="dev",
            budget_usd=1,
            repeats=3,
            client=client,
        )
    report = summarize(dataset, lock, result)
    assert report["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert report["arms"]["jev_head"]["synthetic"]["ndcg10"] is not None
    assert report["arms"]["jev_head"]["real"]["ndcg10"] is None
    result["rows"].pop()
    with pytest.raises(ValueError, match="Incomplete"):
        summarize(dataset, lock, result)


def test_resolved_versions_cannot_be_silently_mixed(dataset: Json, tmp_path: Path) -> None:
    dataset["cases"] = dataset["cases"][:1]
    counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        counter += 1
        response = reply(json.loads(request.content))
        response["model"] = f"version-{counter}"
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        lock = lock_dataset(dataset, PROTOCOL)
        result = run_trial(
            dataset,
            lock,
            tmp_path / "run",
            api_key="fake",
            split="dev",
            budget_usd=1,
            repeats=1,
            client=client,
        )
    assert any(
        "model version" in issue
        for issue in summarize(dataset, lock, result)["insufficient_evidence"]
    )


def test_dataset_mutation_invalidates_results(dataset: Json) -> None:
    lock = lock_dataset(dataset, PROTOCOL)
    dataset["cases"][0]["baseline"].reverse()
    with pytest.raises(ValueError):
        check_lock(dataset, lock)
    assert digest(dataset) != lock["dataset_sha256"]


def test_complete_report_gates_detect_harmful_regression(dataset: Json) -> None:
    """Fabricated results exercise both verdict branches, not model performance."""
    protocol = {
        **PROTOCOL,
        "minimum_real_test_queries": 20,
        "minimum_test_groups": 10,
        "minimum_per_category": 2,
        "bootstrap_samples": 100,
    }
    for c in dataset["cases"]:
        c.update(
            origin="real",
            split="test",
            reviewer="r1",
            second_reviewer="r2",
            reviewed_blind=True,
            capture_verified=True,
        )
    lock = lock_dataset(dataset, protocol)
    rows = []
    for c in dataset["cases"]:
        order = sorted(c["labels"], key=lambda cid: -c["labels"][cid]["grade"])
        for arm in ("jev_head", "jev_passages"):
            for rep in range(3):
                rows.append(
                    {
                        "query_id": c["id"],
                        "arm": arm,
                        "repeat": rep,
                        "order": order,
                        "elapsed_ms": 10,
                        "cost_usd": 0.0001,
                        "cost_known": True,
                        "fallback": False,
                        "abstain": not c["answerable"],
                    }
                )
    result = {
        "dataset_sha256": digest(dataset),
        "protocol_sha256": lock["protocol_sha256"],
        "split": "test",
        "repeats": 3,
        "resolved_models": ["fixed"],
        "rows": rows,
    }
    report = summarize(dataset, lock, result)
    assert report["verdict"] == "GO_TO_SHADOW"
    bad = next(r for r in rows if r["arm"] == "jev_passages")
    bad["order"] = ["d", "a", "b", "c"]
    report = summarize(dataset, lock, result)
    assert not report["gates"]["no_new_harmful_top1"]
    assert report["verdict"] == "NO_GO"


def test_non_object_response_is_safe_fallback(dataset: Json, tmp_path: Path) -> None:
    dataset["cases"] = dataset["cases"][:1]
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )
    with client:
        result = run_trial(
            dataset,
            lock_dataset(dataset, PROTOCOL),
            tmp_path / "run",
            api_key="fake",
            split="dev",
            budget_usd=0.1,
            repeats=1,
            client=client,
        )
    assert all(
        r["fallback"] and r["order"] == dataset["cases"][0]["baseline"] for r in result["rows"]
    )
