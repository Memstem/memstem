"""Tests for `memstem.hygiene.dedup_judge` (ADR 0012 Layer 3 scaffolding).

NO TEST IN THIS FILE CALLS A REAL LLM.

The :class:`OllamaDedupJudge` class is exercised only via mocked HTTP
responses (a fake ``client`` object is passed in). The default judge
in tests is :class:`NoOpJudge` or :class:`StubJudge`, which never
reach out to the network or any model.

We deliberately leave the file path of the prompt template unmocked
so the existence-on-disk test catches regressions in the prompt file.
"""

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
from memstem.hygiene.dedup_candidates import DedupCandidatePair
from memstem.hygiene.dedup_judge import (
    NoOpJudge,
    OllamaDedupJudge,
    StubJudge,
    Verdict,
    _parse_response,
    count_audit_rows,
    judge_pairs,
    write_audit_rows,
)


def _normalized_random(seed: int, dim: int = 768) -> list[float]:
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


def _make_pair(a_id: str = "a", b_id: str = "b") -> DedupCandidatePair:
    return DedupCandidatePair(
        a_id=a_id,
        b_id=b_id,
        cosine=0.9,
        a_title=f"title-{a_id}",
        b_title=f"title-{b_id}",
        a_path=f"memories/{a_id}.md",
        b_path=f"memories/{b_id}.md",
        a_type="memory",
        b_type="memory",
    )


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


class TestVerdictEnum:
    def test_values_match_prompt(self) -> None:
        # The prompt uses these exact category names. If you rename
        # an enum value, update the prompt template too.
        names = {v.value for v in Verdict}
        assert names == {
            "DUPLICATE",
            "CONTRADICTS",
            "RELATED_BUT_DISTINCT",
            "UNRELATED",
        }


class TestNoOpJudge:
    def test_always_returns_unrelated(self) -> None:
        judge = NoOpJudge()
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.UNRELATED
        assert result.judge == "noop"

    def test_carries_pair_ids(self) -> None:
        judge = NoOpJudge()
        result = judge.judge_pair(_make_pair("aaa", "bbb"))
        assert result.new_id == "aaa"
        assert result.existing_id == "bbb"


class TestStubJudge:
    def test_returns_canned_verdict(self) -> None:
        stub = StubJudge()
        stub.set_verdict("a", "b", Verdict.DUPLICATE, "they say the same thing")
        result = stub.judge_pair(_make_pair("a", "b"))
        assert result.verdict is Verdict.DUPLICATE
        assert result.rationale == "they say the same thing"
        assert result.judge == "stub"

    def test_unknown_pair_returns_unrelated(self) -> None:
        # Stub default verdict for un-configured pairs is UNRELATED
        # so unconfigured pairs don't accidentally pass as duplicates.
        stub = StubJudge()
        result = stub.judge_pair(_make_pair("x", "y"))
        assert result.verdict is Verdict.UNRELATED


class TestJudgePairs:
    def test_runs_judge_on_each_pair(self) -> None:
        stub = StubJudge()
        stub.set_verdict("a1", "b1", Verdict.DUPLICATE)
        stub.set_verdict("a2", "b2", Verdict.CONTRADICTS)
        stub.set_verdict("a3", "b3", Verdict.RELATED_BUT_DISTINCT)
        pairs = [_make_pair("a1", "b1"), _make_pair("a2", "b2"), _make_pair("a3", "b3")]
        results = judge_pairs(pairs, judge=stub)
        assert [r.verdict for r in results] == [
            Verdict.DUPLICATE,
            Verdict.CONTRADICTS,
            Verdict.RELATED_BUT_DISTINCT,
        ]

    def test_default_judge_is_noop(self) -> None:
        # Calling judge_pairs without specifying a judge should
        # use NoOpJudge — no LLM invoked, no real network. This is
        # the "safe by default" contract.
        results = judge_pairs([_make_pair("a", "b")])
        assert results[0].judge == "noop"
        assert results[0].verdict is Verdict.UNRELATED

    def test_empty_pairs_returns_empty(self) -> None:
        assert judge_pairs([], judge=NoOpJudge()) == []


class TestAuditLog:
    def test_writes_one_row_per_result(self, index: Index, vault: Vault) -> None:
        # Build real index rows so the audit table can hold the
        # memory_id values.
        m1 = _make_memory(body="alpha", vault=vault)
        m2 = _make_memory(body="alpha", vault=vault)
        index.upsert(m1)
        index.upsert(m2)
        stub = StubJudge()
        stub.set_verdict(str(m1.id), str(m2.id), Verdict.DUPLICATE, "same content")
        results = judge_pairs([_make_pair(str(m1.id), str(m2.id))], judge=stub)
        n = write_audit_rows(index.db, results)
        assert n == 1
        assert count_audit_rows(index.db) == 1
        row = index.db.execute(
            "SELECT verdict, rationale, judge, applied FROM dedup_audit"
        ).fetchone()
        assert row["verdict"] == "DUPLICATE"
        assert row["rationale"] == "same content"
        assert row["judge"] == "stub"
        # `applied = 0` is the contract: this slice never flips it.
        assert row["applied"] == 0

    def test_empty_results_writes_nothing(self, index: Index) -> None:
        n = write_audit_rows(index.db, [])
        assert n == 0
        assert count_audit_rows(index.db) == 0

    def test_multiple_results_persisted(self, index: Index, vault: Vault) -> None:
        # One sweep should be able to write many rows in a batch.
        ids = []
        for _ in range(3):
            m = _make_memory(body="x", vault=vault)
            index.upsert(m)
            ids.append(str(m.id))
        stub = StubJudge()
        stub.set_verdict(ids[0], ids[1], Verdict.DUPLICATE)
        stub.set_verdict(ids[1], ids[2], Verdict.CONTRADICTS)
        pairs = [
            _make_pair(ids[0], ids[1]),
            _make_pair(ids[1], ids[2]),
        ]
        results = judge_pairs(pairs, judge=stub)
        write_audit_rows(index.db, results)
        verdicts = [
            r["verdict"]
            for r in index.db.execute("SELECT verdict FROM dedup_audit ORDER BY id").fetchall()
        ]
        assert verdicts == ["DUPLICATE", "CONTRADICTS"]

    def test_audit_failures_swallowed(self, index: Index, vault: Vault) -> None:
        # Drop the table to force a sqlite error. write_audit_rows
        # must not propagate — the dedup sweep doesn't crash on a
        # broken audit log.
        m1 = _make_memory(body="a", vault=vault)
        m2 = _make_memory(body="b", vault=vault)
        index.upsert(m1)
        index.upsert(m2)
        index.db.execute("DROP TABLE dedup_audit")
        index.db.commit()

        stub = StubJudge()
        stub.set_verdict(str(m1.id), str(m2.id), Verdict.DUPLICATE)
        results = judge_pairs([_make_pair(str(m1.id), str(m2.id))], judge=stub)
        n = write_audit_rows(index.db, results)
        assert n == 0  # write swallowed; no crash


class TestOllamaJudgeMocked:
    """Exercise the OllamaDedupJudge class without a real Ollama call."""

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeClient:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload
            self.last_call: dict[str, object] | None = None

        def post(
            self,
            url: str,
            json: dict[str, object] | None = None,
        ) -> TestOllamaJudgeMocked._FakeResponse:
            self.last_call = {"url": url, "json": json}
            return TestOllamaJudgeMocked._FakeResponse(self.payload)

    def test_parses_well_formed_json(self) -> None:
        client = self._FakeClient(
            {"response": '{"verdict": "DUPLICATE", "rationale": "same fact"}'}
        )
        judge = OllamaDedupJudge(client=client, prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.DUPLICATE
        assert result.rationale == "same fact"
        assert result.judge.startswith("ollama:")
        assert client.last_call is not None

    def test_handles_fenced_json(self) -> None:
        # Models sometimes wrap JSON in code fences.
        client = self._FakeClient(
            {"response": ('```json\n{"verdict": "CONTRADICTS", "rationale": "values differ"}\n```')}
        )
        judge = OllamaDedupJudge(client=client, prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.CONTRADICTS

    def test_garbage_response_falls_back_to_unrelated(self) -> None:
        client = self._FakeClient({"response": "I don't know what to say"})
        judge = OllamaDedupJudge(client=client, prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.UNRELATED

    def test_empty_response_falls_back_to_unrelated(self) -> None:
        client = self._FakeClient({"response": ""})
        judge = OllamaDedupJudge(client=client, prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.UNRELATED

    def test_call_errors_are_caught(self) -> None:
        class _BoomClient:
            def post(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("network down")

        judge = OllamaDedupJudge(client=_BoomClient(), prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.UNRELATED
        assert "network down" in result.rationale

    def test_unknown_verdict_string_is_unrelated(self) -> None:
        client = self._FakeClient({"response": '{"verdict": "MAYBE", "rationale": "shrug"}'})
        judge = OllamaDedupJudge(client=client, prompt_template="ignored")
        result = judge.judge_pair(_make_pair())
        assert result.verdict is Verdict.UNRELATED


class TestParseResponseHelper:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('{"verdict":"DUPLICATE","rationale":"x"}', Verdict.DUPLICATE),
            ('  {"verdict":"CONTRADICTS","rationale":"x"}  ', Verdict.CONTRADICTS),
            (
                '{"verdict":"RELATED_BUT_DISTINCT","rationale":"x"}',
                Verdict.RELATED_BUT_DISTINCT,
            ),
            ('{"verdict":"UNRELATED","rationale":"x"}', Verdict.UNRELATED),
        ],
    )
    def test_known_verdicts(self, raw: str, expected: Verdict) -> None:
        verdict, _ = _parse_response(raw)
        assert verdict is expected

    def test_unknown_value_returns_unrelated(self) -> None:
        verdict, _ = _parse_response('{"verdict":"OTHER","rationale":"x"}')
        assert verdict is Verdict.UNRELATED

    def test_no_json_returns_unrelated(self) -> None:
        verdict, _ = _parse_response("just text")
        assert verdict is Verdict.UNRELATED


class TestPromptTemplate:
    def test_prompt_file_exists_and_has_required_placeholders(self) -> None:
        # The OllamaDedupJudge default prompt loader reads this file;
        # if anything's missing the production path fails. Tests use
        # ``prompt_template="ignored"`` so they don't hit the real
        # file, but we still want a regression test that the file
        # ships with the package.
        path = Path(__file__).parent.parent / "src" / "memstem" / "prompts" / "dedup_judge.txt"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        for marker in (
            "{new_id}",
            "{new_body}",
            "{existing_id}",
            "{existing_body}",
            "DUPLICATE",
            "CONTRADICTS",
            "RELATED_BUT_DISTINCT",
            "UNRELATED",
        ):
            assert marker in text, f"prompt template missing marker {marker!r}"


class TestCli:
    """`memstem hygiene dedup-judge` smoke tests.

    None of these tests pass `--enable-llm`. The CLI default is to use
    NoOpJudge so the audit log is exercised without any network calls.
    """

    def _setup_vault_with_pair(self, tmp_path: Path) -> tuple[Path, Vault, Memory, Memory]:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "_meta" / "config.yaml").write_text(f"vault_path: {root}\n", encoding="utf-8")
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
        return root, v, a, b

    def test_no_pairs_message(self, tmp_path: Path) -> None:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "_meta" / "config.yaml").write_text(f"vault_path: {root}\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "dedup-judge", "--vault", str(root)])
        assert result.exit_code == 0
        assert "no candidate pairs" in result.stdout

    def test_default_writes_noop_audit_rows(self, tmp_path: Path) -> None:
        root, _, _, _ = self._setup_vault_with_pair(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "dedup-judge", "--vault", str(root)])
        assert result.exit_code == 0
        assert "1 NoOp audit row" in result.stdout
        assert "wrote 1 audit row" in result.stdout

        # Confirm the row landed in dedup_audit and applied=0.
        idx = Index(root / "_meta" / "index.db", dimensions=768)
        idx.connect()
        try:
            row = idx.db.execute("SELECT verdict, judge, applied FROM dedup_audit").fetchone()
        finally:
            idx.close()
        assert row is not None
        assert row["verdict"] == "UNRELATED"
        assert row["judge"] == "noop"
        assert row["applied"] == 0

    def test_default_does_not_mutate_vault_frontmatter(self, tmp_path: Path) -> None:
        # The judge must not write deprecated_by / valid_to / supersedes
        # — it only writes audit rows.
        root, v, a, b = self._setup_vault_with_pair(tmp_path)
        before_a = v.read(a.path).frontmatter.model_dump(mode="json")
        before_b = v.read(b.path).frontmatter.model_dump(mode="json")
        runner = CliRunner()
        runner.invoke(app, ["hygiene", "dedup-judge", "--vault", str(root)])
        after_a = v.read(a.path).frontmatter.model_dump(mode="json")
        after_b = v.read(b.path).frontmatter.model_dump(mode="json")
        assert before_a == after_a
        assert before_b == after_b
