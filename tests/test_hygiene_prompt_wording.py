"""A wording-only change to the distillation prompts must not regenerate
stored summaries (review finding M-3).

The session stage picks work by session growth (ADR 0037/0038), never by
prompt text. The project stage used to call the summarizer for every
project on every run and rely on the summarizer cache, keyed on the full
prompt with its wording, to skip unchanged ones; a template edit made
every key miss. It now compares an input hash stored on the record
(``provenance.input_hash``) and, for records written before that field
existed, the cached output's timestamp.

These tests drive the hygiene loop's own stage methods with a counting
summarizer: distill under the current templates, change their wording,
run again, count calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import memstem.hygiene.project_records as pr
import memstem.hygiene.session_distill as sd
from memstem.config import HygieneConfig
from memstem.core.frontmatter import MemoryType, validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.core.summarizer import Summarizer
from memstem.hygiene.loop import HygieneLoop

SESSION_TEMPLATE = sd._load_session_prompt()
PROJECT_TEMPLATE = pr._load_project_prompt()


def _reworded(template: str) -> str:
    # The kind of edit M-3 is about: the example customer renamed.
    changed = template.replace("Northwind", "Example Co")
    assert changed != template
    return changed


class CountingSummarizer(Summarizer):
    name = "test:counting"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"# Project title {len(self.prompts)}\n\nSummary {len(self.prompts)}."


def _write_session(vault: Vault, sid: str, tag: str, when: datetime) -> None:
    turns: list[str] = []
    for i in range(12):
        turns.append(f"**User:** question {i} about the {tag} deliverable and its scope")
        turns.append(f"**Assistant:** answer {i} covering the plan, the risks and next steps")
    fm = validate(
        {
            "id": str(uuid4()),
            "type": "session",
            "created": when.isoformat(),
            "updated": when.isoformat(),
            "source": "claude-code",
            "title": f"session {sid}",
            "tags": [tag],
        }
    )
    vault.write(Memory(frontmatter=fm, body="\n\n".join(turns), path=Path(f"sessions/{sid}.md")))


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories/projects", "skills", "sessions", "daily", "distillations", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


@pytest.fixture
def index(vault: Vault) -> Iterator[Index]:
    idx = Index(vault.root / "_meta" / "index.db")
    idx.connect()
    yield idx
    idx.close()


@pytest.fixture
def templates(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    current = {"session": SESSION_TEMPLATE, "project": PROJECT_TEMPLATE}
    monkeypatch.setattr(sd, "_load_session_prompt", lambda: current["session"])
    monkeypatch.setattr(pr, "_load_project_prompt", lambda: current["project"])
    return current


@pytest.fixture
def summarizer() -> CountingSummarizer:
    return CountingSummarizer()


@pytest.fixture
def loop(
    vault: Vault, index: Index, summarizer: CountingSummarizer, templates: dict[str, str]
) -> HygieneLoop:
    now = datetime.now(tz=UTC)
    for p in range(3):
        for s in range(2):
            _write_session(
                vault, f"p{p}s{s}", f"home-user-project-{p}", now - timedelta(hours=p + s)
            )
    _write_session(vault, "solo", "home-user-solo", now - timedelta(hours=9))
    hygiene = HygieneLoop(vault, index, HygieneConfig())
    hygiene._summarizer = summarizer
    return hygiene


def _run(loop: HygieneLoop, summarizer: CountingSummarizer) -> tuple[int, int]:
    """Run the session and project stages; return their call counts."""
    start = len(summarizer.prompts)
    loop._run_distill_sessions()
    mid = len(summarizer.prompts)
    loop._run_project_records()
    return mid - start, len(summarizer.prompts) - mid


def _project_records(vault: Vault) -> list[Memory]:
    return list(vault.walk(types=[MemoryType.PROJECT.value]))


def _strip_input_hash(vault: Vault) -> None:
    """Make every project record look written by a release before the field."""
    for record in _project_records(vault):
        data = record.frontmatter.model_dump(mode="json", exclude_none=True)
        data["provenance"].pop(pr.PROVENANCE_INPUT_HASH, None)
        vault.write(Memory(frontmatter=validate(data), body=record.body, path=record.path))


def test_first_run_distills_everything(loop: HygieneLoop, summarizer: CountingSummarizer) -> None:
    assert _run(loop, summarizer) == (7, 3)
    assert _run(loop, summarizer) == (0, 0)


def test_wording_change_makes_no_model_calls(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
) -> None:
    _run(loop, summarizer)
    bodies = {r.path: r.body for r in _project_records(vault)}
    templates["session"] = _reworded(SESSION_TEMPLATE)
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    assert _run(loop, summarizer) == (0, 0)
    assert {r.path: r.body for r in _project_records(vault)} == bodies


def test_records_from_before_the_input_hash_are_not_regenerated(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
) -> None:
    _run(loop, summarizer)
    _strip_input_hash(vault)
    templates["session"] = _reworded(SESSION_TEMPLATE)
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    assert _run(loop, summarizer) == (0, 0)
    # The run stamped the hash, so the next check no longer needs the cache.
    for record in _project_records(vault):
        assert record.frontmatter.provenance is not None
        extra = record.frontmatter.provenance.model_extra or {}
        assert extra.get(pr.PROVENANCE_INPUT_HASH)


def test_legacy_record_with_a_later_source_update_is_regenerated(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
) -> None:
    _run(loop, summarizer)
    _strip_input_hash(vault)
    session = vault.read("sessions/p1s0.md")
    data = session.frontmatter.model_dump(mode="json", exclude_none=True)
    data["updated"] = (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()
    vault.write(Memory(frontmatter=validate(data), body=session.body, path=session.path))
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    assert _run(loop, summarizer) == (0, 1)


def test_legacy_record_is_regenerated_after_a_version_bump(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: a record from before the input hash used to be kept
    # and stamped with the new hash whatever the version said.
    _run(loop, summarizer)
    _strip_input_hash(vault)
    bodies = {r.path: r.body for r in _project_records(vault)}
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    monkeypatch.setattr(pr, "PROJECT_PROMPT_VERSION", pr.PROJECT_PROMPT_VERSION + 1)
    assert _run(loop, summarizer) == (0, 3)
    after = {r.path: r.body for r in _project_records(vault)}
    assert all(after[path] != body for path, body in bodies.items())


def test_legacy_record_is_not_kept_as_unchanged_after_a_version_bump(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    vault: Vault,
    index: Index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(loop, summarizer)
    _strip_input_hash(vault)
    monkeypatch.setattr(pr, "PROJECT_PROMPT_VERSION", pr.PROJECT_PROMPT_VERSION + 1)
    plan = pr.compute_project_record_plan(vault, summarizer, db=index.db, lock=index.lock)
    assert not any(p.unchanged for p in plan.proposals)


def _stamp(when: datetime, naive: bool) -> str:
    return (when.replace(tzinfo=None) if naive else when).isoformat()


def _set_source_updated(vault: Vault, when: datetime, *, naive: bool) -> None:
    """Set ``updated`` on every session and distillation."""
    for kind in ("session", "distillation"):
        for record in vault.walk(types=[kind]):
            data = record.frontmatter.model_dump(mode="json", exclude_none=True)
            data["updated"] = _stamp(when, naive)
            fm = validate(data)
            assert (fm.updated.tzinfo is None) == naive
            vault.write(Memory(frontmatter=fm, body=record.body, path=record.path))


def _set_cache_ts(index: Index, when: datetime, *, naive: bool) -> None:
    with index.db:
        index.db.execute("UPDATE summarizer_cache SET ts = ?", (_stamp(when, naive),))


@pytest.mark.parametrize(
    ("naive_source", "naive_cache"), [(True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("source_is_later", [False, True])
def test_legacy_check_compares_naive_and_aware_timestamps(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
    index: Index,
    naive_source: bool,
    naive_cache: bool,
    source_is_later: bool,
) -> None:
    # Review finding: a naive ``updated`` against an aware cache time (or
    # the reverse) raised TypeError on every run. Naive values are UTC.
    _run(loop, summarizer)
    _strip_input_hash(vault)
    cached_at = datetime.now(tz=UTC) + timedelta(hours=1)
    offset = timedelta(minutes=5) if source_is_later else -timedelta(days=1)
    _set_cache_ts(index, cached_at, naive=naive_cache)
    _set_source_updated(vault, cached_at + offset, naive=naive_source)
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    # Only the project stage: the session stage is not part of this check.
    start = len(summarizer.prompts)
    loop._run_project_records()
    assert len(summarizer.prompts) - start == (3 if source_is_later else 0)


def test_new_input_still_regenerates_its_project(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    vault: Vault,
) -> None:
    _run(loop, summarizer)
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    _write_session(vault, "p0s2", "home-user-project-0", datetime.now(tz=UTC))
    assert _run(loop, summarizer) == (1, 1)
    assert "Example Co" in summarizer.prompts[-1]


def test_version_bump_regenerates_under_the_new_wording(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    templates: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(loop, summarizer)
    templates["project"] = _reworded(PROJECT_TEMPLATE)
    monkeypatch.setattr(pr, "PROJECT_PROMPT_VERSION", pr.PROJECT_PROMPT_VERSION + 1)
    assert _run(loop, summarizer) == (0, 3)


def test_version_bump_without_wording_change_reuses_the_cache(
    loop: HygieneLoop,
    summarizer: CountingSummarizer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(loop, summarizer)
    monkeypatch.setattr(pr, "PROJECT_PROMPT_VERSION", pr.PROJECT_PROMPT_VERSION + 1)
    assert _run(loop, summarizer) == (0, 0)


def test_force_resummarizes(
    loop: HygieneLoop, summarizer: CountingSummarizer, vault: Vault, index: Index
) -> None:
    _run(loop, summarizer)
    start = len(summarizer.prompts)
    plan = pr.compute_project_record_plan(vault, summarizer, db=index.db, force=True)
    assert len(summarizer.prompts) - start == 3
    assert not any(p.unchanged for p in plan.proposals)


def test_summarizer_change_regenerates(
    loop: HygieneLoop, summarizer: CountingSummarizer, vault: Vault, index: Index
) -> None:
    _run(loop, summarizer)
    other = CountingSummarizer()
    other.name = "test:other-model"
    pr.compute_project_record_plan(vault, other, db=index.db)
    assert len(other.prompts) == 3
