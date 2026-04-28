"""Layer 3 of ADR 0012's dedup pipeline: LLM-as-judge + audit log.

This module ships **scaffolding only** — the structural pieces an
operator and a future PR need to apply judge verdicts as resolution
actions, but no destructive action is performed here. Specifically:

- **No mutation.** Verdicts are written to the ``dedup_audit`` table
  with ``applied = 0``. The future resolution PR will look up
  ``applied = 0`` rows, apply them (writing ``deprecated_by`` /
  ``valid_to`` / ``supersedes`` / ``links`` to vault frontmatter),
  and flip ``applied = 1`` to mark them done.
- **No real LLM in the test path.** The :class:`DedupJudge` ABC and
  the :class:`NoOpJudge` default implementation are pure Python.
  :class:`OllamaDedupJudge` exists for production use behind the
  explicit ``--enable-llm`` flag, but tests never instantiate it —
  they pass stub judges that return canned verdicts.
- **Default-off LLM.** The CLI command runs with :class:`NoOpJudge`
  unless the operator passes ``--enable-llm``. Without the flag,
  the audit table accumulates ``UNRELATED`` rows that record "we
  saw this candidate pair, no LLM was consulted" — useful as an
  inventory step but harmless.

The contract is: this module reads from and writes to the audit
table; it never touches vault frontmatter or the canonical
``memories`` table. ADR 0012 PR-D ("Resolution actions") is the
only piece that should do that, and only when a future PR adds
explicit ``--apply`` semantics on a per-verdict basis.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from memstem.hygiene.dedup_candidates import DedupCandidatePair

logger = logging.getLogger(__name__)


class Verdict(StrEnum):
    """The four-way classification ADR 0012's prompt asks the LLM to choose.

    The values are uppercase (matching the prompt's category names)
    so audit-log readers can grep raw rows without normalization.
    """

    DUPLICATE = "DUPLICATE"
    CONTRADICTS = "CONTRADICTS"
    RELATED_BUT_DISTINCT = "RELATED_BUT_DISTINCT"
    UNRELATED = "UNRELATED"


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """One verdict for one candidate pair.

    The ``judge`` string identifies which implementation produced
    this verdict (``"noop"``, ``"ollama:qwen2.5:7b"``, ``"stub"``)
    so the audit log can distinguish a real LLM call from a fallback.
    """

    new_id: str
    existing_id: str
    verdict: Verdict
    rationale: str
    judge: str


class DedupJudge:
    """Abstract base for dedup judges.

    Subclasses override :meth:`judge_pair`. The contract is intentionally
    narrow — one pair in, one verdict out — so callers can swap real
    LLMs for mocks without changing the orchestration.

    Subclasses MUST set :attr:`name` to a stable identifier that ends
    up in the audit log's ``judge`` column.
    """

    name: str = "abstract"

    def judge_pair(self, pair: DedupCandidatePair) -> JudgeResult:
        raise NotImplementedError


class NoOpJudge(DedupJudge):
    """Default fallback judge — always returns UNRELATED.

    Used when no LLM is configured or when the operator wants to
    populate the audit log without spending LLM cycles. The ``UNRELATED``
    verdict means "no opinion expressed"; a future operator running the
    real LLM judge will overwrite these audit rows with real verdicts.
    """

    name = "noop"

    def judge_pair(self, pair: DedupCandidatePair) -> JudgeResult:
        return JudgeResult(
            new_id=pair.a_id,
            existing_id=pair.b_id,
            verdict=Verdict.UNRELATED,
            rationale="no judge configured (NoOpJudge fallback)",
            judge=self.name,
        )


class StubJudge(DedupJudge):
    """In-memory judge for tests. Returns whatever :meth:`set_verdict` configured.

    Tests register canned (pair_key → verdict) entries and call the
    orchestration; the stub's :meth:`judge_pair` looks up the
    configured verdict. This keeps test fixtures local and obvious:
    the test sees exactly what the stub will return for each pair.
    """

    name = "stub"

    def __init__(self) -> None:
        self._verdicts: dict[tuple[str, str], tuple[Verdict, str]] = {}

    def set_verdict(
        self,
        a_id: str,
        b_id: str,
        verdict: Verdict,
        rationale: str = "stub verdict",
    ) -> None:
        """Configure the verdict the stub will return for one pair."""
        self._verdicts[(a_id, b_id)] = (verdict, rationale)

    def judge_pair(self, pair: DedupCandidatePair) -> JudgeResult:
        verdict, rationale = self._verdicts.get(
            (pair.a_id, pair.b_id),
            (Verdict.UNRELATED, "stub default"),
        )
        return JudgeResult(
            new_id=pair.a_id,
            existing_id=pair.b_id,
            verdict=verdict,
            rationale=rationale,
            judge=self.name,
        )


class OllamaDedupJudge(DedupJudge):
    """Live judge that calls a local Ollama model with the dedup prompt.

    Behind explicit operator opt-in (``--enable-llm`` on the CLI).
    Tests must NOT instantiate this — they use :class:`NoOpJudge` or
    :class:`StubJudge`. The constructor accepts an explicit ``client``
    callable so the integration is at least mockable if a future test
    wants to.

    The model is expected to return strict JSON of the form
    ``{"verdict": "...", "rationale": "..."}``. Anything else is
    parsed as ``UNRELATED`` with the raw response in the rationale —
    we never crash the sweep on a malformed response.
    """

    name_prefix = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        prompt_template: str | None = None,
        client: object = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.prompt_template = prompt_template or _load_prompt_template()
        self._client = client
        self.name = f"{self.name_prefix}:{model}"

    def _http_client(self) -> object:
        if self._client is None:
            # Lazy httpx import so the module can be imported without
            # the dependency at collection time. httpx is already in
            # the project's deps.
            import httpx

            self._client = httpx.Client(base_url=self.base_url, timeout=60.0)
        return self._client

    def judge_pair(self, pair: DedupCandidatePair) -> JudgeResult:
        # Bodies travel via the path's title in the candidate pair —
        # the candidate generator was deliberately stingy and didn't
        # ship full bodies. Future versions of OllamaDedupJudge would
        # accept a vault and read the bodies; for now we use what's
        # in the pair (titles + ids) so the shape is correct.
        prompt = self.prompt_template.format(
            new_id=pair.a_id,
            new_body=(pair.a_title or pair.a_id),
            existing_id=pair.b_id,
            existing_body=(pair.b_title or pair.b_id),
        )
        try:
            response = self._call_model(prompt)
            verdict, rationale = _parse_response(response)
        except Exception as exc:
            logger.warning("OllamaDedupJudge: model call failed: %s", exc)
            return JudgeResult(
                new_id=pair.a_id,
                existing_id=pair.b_id,
                verdict=Verdict.UNRELATED,
                rationale=f"model call failed: {exc}",
                judge=self.name,
            )
        return JudgeResult(
            new_id=pair.a_id,
            existing_id=pair.b_id,
            verdict=verdict,
            rationale=rationale,
            judge=self.name,
        )

    def _call_model(self, prompt: str) -> str:
        client = self._http_client()
        # Ollama /api/generate returns ``{"response": "..."}``.
        post = client.post  # type: ignore[attr-defined]
        result = post(
            "/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
        )
        result.raise_for_status()
        body = result.json()
        return str(body.get("response", ""))


def _load_prompt_template() -> str:
    """Read the canonical dedup judge prompt from the package data."""
    path = Path(__file__).parent.parent / "prompts" / "dedup_judge.txt"
    return path.read_text(encoding="utf-8")


def _parse_response(text: str) -> tuple[Verdict, str]:
    """Permissively parse the LLM's JSON response.

    Accepts a JSON object embedded in a longer string (the model
    sometimes wraps the JSON in a code fence). Falls back to
    ``UNRELATED`` with the raw text in the rationale on any parse
    failure — the audit log surfaces what went wrong, the operator
    re-runs if needed.
    """
    if not text:
        return Verdict.UNRELATED, "empty model response"
    candidates = _extract_json_substrings(text)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        verdict_str = str(data.get("verdict", "")).strip().upper()
        rationale_str = str(data.get("rationale", "")).strip() or "no rationale"
        if verdict_str in {v.value for v in Verdict}:
            return Verdict(verdict_str), rationale_str
        # Known verdict but not in our enum — defensive: log and
        # treat as UNRELATED.
        return Verdict.UNRELATED, f"unknown verdict {verdict_str!r}: {rationale_str}"
    snippet = text[:200].replace("\n", " ")
    return Verdict.UNRELATED, f"could not parse model response: {snippet!r}"


def _extract_json_substrings(text: str) -> list[str]:
    """Return candidate JSON object substrings from ``text``.

    Handles fenced code blocks (```json ... ```) and bare JSON. We
    don't try to handle every malformed model output — just the
    common shapes.
    """
    out: list[str] = []
    # First pass: try the whole text.
    out.append(text.strip())
    # Second pass: extract first { ... } pair.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        out.append(text[start : end + 1])
    return out


def judge_pairs(
    pairs: list[DedupCandidatePair],
    judge: DedupJudge | None = None,
) -> list[JudgeResult]:
    """Run ``judge`` over each pair, returning verdicts.

    ``judge`` defaults to :class:`NoOpJudge`, which means callers
    that don't explicitly opt in to an LLM still get a usable
    output (every pair gets ``UNRELATED``). The orchestration is
    intentionally trivial — there's no batching, retry, or rate-
    limiting because dedup is not on a hot path.
    """
    judge = judge or NoOpJudge()
    return [judge.judge_pair(pair) for pair in pairs]


def write_audit_rows(
    db: sqlite3.Connection,
    results: list[JudgeResult],
    *,
    now: datetime | None = None,
) -> int:
    """Append ``results`` to the ``dedup_audit`` table. Returns rows written.

    Every row is written with ``applied = 0``. The resolution PR
    that flips ``applied = 1`` lives outside this slice.
    """
    if not results:
        return 0
    timestamp = (now or datetime.now(tz=UTC)).isoformat()
    rows = [
        (
            timestamp,
            r.new_id,
            r.existing_id,
            r.verdict.value,
            r.rationale,
            r.judge,
            0,
        )
        for r in results
    ]
    try:
        with db:
            db.executemany(
                """
                INSERT INTO dedup_audit
                    (ts, new_id, existing_id, verdict, rationale, judge, applied)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    except sqlite3.Error as exc:
        logger.warning("dedup_audit: failed to write %d row(s): %s", len(rows), exc)
        return 0
    return len(rows)


def count_audit_rows(db: sqlite3.Connection) -> int:
    """Total rows currently in ``dedup_audit`` (for debugging / tests)."""
    row = db.execute("SELECT COUNT(*) FROM dedup_audit").fetchone()
    if row is None:
        return 0
    return int(row[0])


__all__ = [
    "DedupJudge",
    "JudgeResult",
    "NoOpJudge",
    "OllamaDedupJudge",
    "StubJudge",
    "Verdict",
    "count_audit_rows",
    "judge_pairs",
    "write_audit_rows",
]
