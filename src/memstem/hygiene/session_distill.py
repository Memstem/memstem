"""Session distillation writer (ADR 0020 — the writer slice of PR-D).

The candidate report (``hygiene/distillation.py``) clusters memories
by topic tag or daily-week. This module is its sibling for the simpler
shape: one session in, one ``type: distillation`` companion record
out, link-back via the ``links`` frontmatter field.

The flow follows the planner / applier pattern from
``hygiene/importance.py``:

1. :func:`find_session_candidates` walks the vault, applies the
   meaningfulness threshold (turn count + word count), and skips
   sessions that already have a linked distillation.
2. :func:`compute_distillation_plan` calls the configured
   :class:`~memstem.core.summarizer.Summarizer` on each candidate via
   the cache-aware ``generate_cached`` orchestrator, returning a plan
   the CLI can preview in ``--dry-run`` mode.
3. :func:`apply_distillations` writes the ``type: distillation``
   Memory records to the vault and upserts them into the index.

Design notes:

- **Per-session shape, not per-cluster.** ADR 0020's writer ships the
  simpler shape first; topic-cluster distillation (ADR 0008 PR-E)
  remains a separate slice.
- **Provenance is mandatory.** Every distillation links back to its
  source session via ``frontmatter.links`` and
  ``frontmatter.provenance.ref`` (``"session-distillation:<session_id>"``).
- **Idempotent re-runs.** The default candidate filter excludes
  sessions whose distillation already exists. ``--force`` regenerates
  anyway.
- **Path stem matches source.** A session at
  ``sessions/<id>.md`` produces a distillation at
  ``distillations/<source>/<id>.md`` (or
  ``distillations/<source>/<agent>/<id>.md`` for OpenClaw memories
  carrying an ``agent:<tag>`` tag). Same stem makes the relationship
  visible from a directory listing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from memstem.core.frontmatter import Frontmatter, MemoryType, validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.core.summarizer import Summarizer

logger = logging.getLogger(__name__)


DEFAULT_MIN_TURNS = 10
"""ADR 0020 §Meaningfulness threshold: a session is eligible iff it
has at least this many user/assistant turns (or, when the
``turn_count`` metadata field is missing, this many sentences)."""

DEFAULT_MIN_WORDS = 100
"""ADR 0020 §Meaningfulness threshold: a session is eligible iff its
body is at least this many words. Catches "10 turns of one-line
replies" sessions that pass the turn-count gate but have no real
content to summarize."""

DEFAULT_RECENCY_DAYS = 30
"""Default recency window for the candidate scan. ``--backfill``
disables this filter (every session is a candidate)."""

DEFAULT_DISTILLATION_IMPORTANCE = 0.8
"""ADR 0020 §Search ranking: distillation seeds get this importance
value so the existing ``alpha=0.2`` multiplier boosts them ~16% above
neutral (0.5) records — enough to outrank a raw session of similar
relevance, not enough to bulldoze a directly-matching skill."""

PROVENANCE_REF_PREFIX = "session-distillation:"
"""Prefix on ``frontmatter.provenance.ref`` for session distillations.
Lets the writer recognize distillations it produced (vs. ones the
operator may have authored manually) when scanning for already-
distilled sessions."""

DISTILLATION_KIND_TAG = "distillation:session"
"""Static tag added to every session distillation. Lets agents filter
``types=[distillation]`` and additionally narrow to session-shape
distillations vs (future) topic-cluster ones."""


# ─── Data classes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionCandidate:
    """One session that's eligible for distillation.

    Carries everything the prompt builder + path computer need so we
    don't re-read the source Memory at apply time. ``body`` is the
    full session transcript; the prompt template handles truncation if
    needed (the LLM provider's context window is the binding limit).
    """

    memory_id: str
    title: str
    body: str
    tags: list[str]
    source: str
    agent: str | None
    session_id: str
    turn_count: int
    word_count: int
    created: datetime
    updated: datetime


@dataclass(frozen=True)
class DistillationProposal:
    """One planned distillation: candidate + LLM output.

    ``summary`` is empty when the configured summarizer returned the
    empty string (NoOp fallback, LLM unreachable, etc.). The applier
    skips empty summaries — that's the safe failure mode.

    ``skipped_reason`` is non-None when the proposal was skipped at
    plan time (e.g. the LLM returned empty). Lets ``--dry-run``
    explain *why* a candidate didn't produce a distillation.
    """

    candidate: SessionCandidate
    summary: str
    summarizer_name: str
    skipped_reason: str | None = None


@dataclass(frozen=True)
class DistillationPlan:
    """Full sweep result, returned by :func:`compute_distillation_plan`."""

    proposals: list[DistillationProposal] = field(default_factory=list)
    total_sessions_scanned: int = 0
    skipped_already_distilled: int = 0
    skipped_too_short: int = 0


@dataclass
class ApplyResult:
    """Side-effect summary, returned by :func:`apply_distillations`."""

    written: int = 0
    skipped_no_summary: int = 0
    apply_errors: list[str] = field(default_factory=list)


# ─── Discovery ────────────────────────────────────────────────────


def _session_id_from_link(link: str) -> str | None:
    """Pull the session id out of a ``memory://sessions/<id>`` link.

    Returns ``None`` for any other link shape so unrelated frontmatter
    links don't accidentally mark sessions as distilled.
    """
    if not link:
        return None
    stripped = link.strip()
    # Accept both `memory://sessions/<id>` and bare `sessions/<id>` shapes.
    for prefix in ("memory://sessions/", "sessions/"):
        if stripped.startswith(prefix):
            tail = stripped[len(prefix) :]
            if tail.endswith(".md"):
                tail = tail[: -len(".md")]
            return tail or None
    return None


def find_distilled_session_ids(vault: Vault) -> set[str]:
    """Return the set of session ids already covered by a distillation.

    Walks every ``type: distillation`` record in the vault and pulls
    the source session ids out of its ``links`` field. Used by
    :func:`find_session_candidates` to skip sessions that already
    have a companion distillation, making re-runs cheap.
    """
    covered: set[str] = set()
    for memory in vault.walk(types=[MemoryType.DISTILLATION.value]):
        for link in memory.frontmatter.links:
            session_id = _session_id_from_link(link)
            if session_id:
                covered.add(session_id)
    return covered


def _word_count(body: str) -> int:
    return len(body.split())


def _agent_tag(memory: Memory) -> str | None:
    for tag in memory.frontmatter.tags:
        if tag.startswith("agent:"):
            stripped = tag[len("agent:") :].strip()
            if stripped:
                return stripped
    return None


def is_meaningful_session(
    memory: Memory,
    *,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_words: int = DEFAULT_MIN_WORDS,
) -> bool:
    """Return True iff ``memory`` clears both threshold gates.

    ``turn_count`` is read from ``provenance.extra`` when the adapter
    stamped it (Claude Code adapter does this); when missing, we
    approximate by counting "**User:**" / "**Assistant:**" turn
    markers in the body.
    """
    if memory.type is not MemoryType.SESSION:
        return False
    body = memory.body or ""
    if _word_count(body) < min_words:
        return False
    turn_count = _extract_turn_count(memory)
    return turn_count >= min_turns


def _extract_turn_count(memory: Memory) -> int:
    """Best-effort turn count from frontmatter or body markers."""
    fm: Frontmatter = memory.frontmatter
    extra = getattr(fm, "model_extra", None) or {}
    raw = extra.get("turn_count")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            pass
    # Provenance can carry it too — older records may have stashed it
    # under provenance.
    prov = fm.provenance
    if prov is not None:
        prov_extra = getattr(prov, "model_extra", None) or {}
        prov_raw = prov_extra.get("turn_count")
        if isinstance(prov_raw, int):
            return prov_raw
    # Fallback: count turn markers in the body. Each turn is one
    # "**User:**" or "**Assistant:**" line at the start of a paragraph.
    body = memory.body or ""
    count = 0
    for line in body.splitlines():
        s = line.lstrip()
        if s.startswith("**User:**") or s.startswith("**Assistant:**"):
            count += 1
    return count


def _candidate_from_memory(memory: Memory) -> SessionCandidate:
    """Project a session :class:`Memory` into a :class:`SessionCandidate`.

    The session_id is derived from the path stem. The Claude Code
    adapter writes sessions to ``sessions/<session_id>.md`` and the
    OpenClaw adapter does the same for trajectory files, so the stem
    is always the canonical session id.
    """
    session_id = Path(memory.path).stem
    return SessionCandidate(
        memory_id=str(memory.id),
        title=memory.frontmatter.title or "(untitled)",
        body=memory.body or "",
        tags=list(memory.frontmatter.tags),
        source=memory.frontmatter.source,
        agent=_agent_tag(memory),
        session_id=session_id,
        turn_count=_extract_turn_count(memory),
        word_count=_word_count(memory.body or ""),
        created=memory.frontmatter.created,
        updated=memory.frontmatter.updated,
    )


def find_session_candidates(
    vault: Vault,
    *,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_words: int = DEFAULT_MIN_WORDS,
    recency_days: int | None = DEFAULT_RECENCY_DAYS,
    include_already_distilled: bool = False,
    now: datetime | None = None,
) -> tuple[list[SessionCandidate], dict[str, int]]:
    """Walk vault sessions, filter, and return candidates + skip counts.

    Returns ``(candidates, stats)`` where ``stats`` carries:
    - ``total_sessions_scanned``: number of session records walked.
    - ``skipped_already_distilled``: sessions filtered by the
      already-covered set (when ``include_already_distilled=False``).
    - ``skipped_too_short``: sessions failing the turn/word threshold.

    ``recency_days=None`` disables the recency filter (the
    ``--backfill`` mode); pass an integer N to limit candidates to
    sessions whose ``updated`` is within the last N days.
    """
    cutoff: datetime | None = None
    if recency_days is not None:
        cutoff = (now or datetime.now(tz=UTC)) - timedelta(days=recency_days)

    covered = set() if include_already_distilled else find_distilled_session_ids(vault)

    candidates: list[SessionCandidate] = []
    total = 0
    skipped_already = 0
    skipped_short = 0
    for memory in vault.walk(types=[MemoryType.SESSION.value]):
        total += 1
        # Cheap recency filter first — the threshold check parses the
        # body, so doing the date check up front saves work on big vaults.
        if cutoff is not None and memory.frontmatter.updated < cutoff:
            continue
        session_id = Path(memory.path).stem
        if session_id in covered:
            skipped_already += 1
            continue
        if not is_meaningful_session(memory, min_turns=min_turns, min_words=min_words):
            skipped_short += 1
            continue
        candidates.append(_candidate_from_memory(memory))

    stats = {
        "total_sessions_scanned": total,
        "skipped_already_distilled": skipped_already,
        "skipped_too_short": skipped_short,
    }
    return candidates, stats


# ─── Prompt construction ──────────────────────────────────────────


def _load_session_prompt() -> str:
    """Read the canonical session-distillation prompt template."""
    path = Path(__file__).parent.parent / "prompts" / "distill_session.txt"
    return path.read_text(encoding="utf-8")


def build_session_prompt(candidate: SessionCandidate, *, prompt_template: str | None = None) -> str:
    """Render the session-distillation prompt for one candidate.

    Pure function — given the same candidate it produces the same
    string, which means the summarizer cache key is deterministic for
    a given candidate + template combination.
    """
    template = prompt_template or _load_session_prompt()
    tag_line = ", ".join(candidate.tags) if candidate.tags else "(none)"
    return template.format(
        title=candidate.title,
        tags=tag_line,
        body=candidate.body,
    )


# ─── Materialization ──────────────────────────────────────────────


def _distillation_path(candidate: SessionCandidate) -> Path:
    parts: list[str] = ["distillations", candidate.source]
    if candidate.agent:
        parts.append(candidate.agent)
    parts.append(f"{candidate.session_id}.md")
    return Path(*parts)


def _inherit_tags(candidate: SessionCandidate) -> list[str]:
    """Compute the tag list for a distillation record.

    Inherits ``agent:*`` and project-tag content from the source so
    searches scoped to a tag still surface the distillation. Adds the
    static ``distillation:session`` marker so agents can filter
    session-shape distillations without scanning every distillation
    record.
    """
    tags = list(candidate.tags)
    if DISTILLATION_KIND_TAG not in tags:
        tags.append(DISTILLATION_KIND_TAG)
    return tags


def materialize_distillation(
    candidate: SessionCandidate,
    summary: str,
    summarizer_name: str,
    *,
    importance: float = DEFAULT_DISTILLATION_IMPORTANCE,
    now: datetime | None = None,
    memory_id: str | None = None,
) -> Memory:
    """Build the ``type: distillation`` :class:`Memory` for a candidate.

    The body is the LLM-produced summary verbatim. The frontmatter
    carries:

    - ``links`` pointing back to the source session (mandatory
      provenance per ADR 0020).
    - ``provenance.ref`` shaped as ``session-distillation:<session_id>``
      so the writer can identify its own outputs on re-runs.
    - ``importance`` seeded to :data:`DEFAULT_DISTILLATION_IMPORTANCE`
      unless overridden, so the search-time multiplier surfaces the
      distillation above raw transcripts on close ties.
    - ``tags`` inherited from the source (so a search for the project
      tag still matches the distillation) plus a static
      ``distillation:session`` marker.
    """
    timestamp = now or datetime.now(tz=UTC)
    new_id = memory_id or str(uuid4())
    title = f"Distillation — {candidate.title}"
    payload: dict[str, Any] = {
        "id": new_id,
        "type": MemoryType.DISTILLATION.value,
        "created": timestamp.isoformat(),
        "updated": timestamp.isoformat(),
        "source": "hygiene-worker",
        "title": title,
        "tags": _inherit_tags(candidate),
        "links": [f"memory://sessions/{candidate.session_id}"],
        "importance": importance,
        "provenance": {
            "source": "hygiene-worker",
            "ref": f"{PROVENANCE_REF_PREFIX}{candidate.session_id}",
            "ingested_at": timestamp.isoformat(),
            "summarizer": summarizer_name,
        },
    }
    fm = validate(payload)
    return Memory(frontmatter=fm, body=summary, path=_distillation_path(candidate))


# ─── Plan + apply ─────────────────────────────────────────────────


def compute_distillation_plan(
    vault: Vault,
    summarizer: Summarizer,
    *,
    db: object | None = None,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_words: int = DEFAULT_MIN_WORDS,
    recency_days: int | None = DEFAULT_RECENCY_DAYS,
    force: bool = False,
    prompt_template: str | None = None,
    now: datetime | None = None,
) -> DistillationPlan:
    """Build the full distillation plan against the configured summarizer.

    ``force=True`` rewrites distillations even when one already
    exists for a session (mirrors ``--force`` on the CLI). The default
    skips sessions covered by an existing ``type: distillation``
    record so re-runs are cheap.

    The summarizer's :meth:`generate_cached` is called with ``db`` so
    repeated runs short-circuit on cached output. ``db=None`` skips
    the cache entirely (useful in tests / one-shot invocations).
    """
    candidates, stats = find_session_candidates(
        vault,
        min_turns=min_turns,
        min_words=min_words,
        recency_days=recency_days,
        include_already_distilled=force,
        now=now,
    )

    proposals: list[DistillationProposal] = []
    for candidate in candidates:
        prompt = build_session_prompt(candidate, prompt_template=prompt_template)
        # ``db`` is sqlite3.Connection in production but typed as
        # ``object`` here so callers can pass ``None`` without import
        # gymnastics.
        summary = summarizer.generate_cached(prompt, db=db)  # type: ignore[arg-type]
        skipped_reason: str | None = None
        if not summary:
            skipped_reason = "summarizer returned empty (NoOp default or LLM unreachable)"
        proposals.append(
            DistillationProposal(
                candidate=candidate,
                summary=summary,
                summarizer_name=summarizer.name,
                skipped_reason=skipped_reason,
            )
        )

    return DistillationPlan(
        proposals=proposals,
        total_sessions_scanned=stats["total_sessions_scanned"],
        skipped_already_distilled=stats["skipped_already_distilled"],
        skipped_too_short=stats["skipped_too_short"],
    )


def _existing_distillation_for_session(vault: Vault, session_id: str) -> Memory | None:
    """Return the existing distillation for ``session_id``, if any.

    Used by ``--force`` re-runs to overwrite the prior record at the
    same path while preserving its memory_id (so the index doesn't
    accumulate orphaned rows).
    """
    for memory in vault.walk(types=[MemoryType.DISTILLATION.value]):
        for link in memory.frontmatter.links:
            if _session_id_from_link(link) == session_id:
                return memory
    return None


def apply_distillations(
    vault: Vault,
    index: Index,
    plan: DistillationPlan,
    *,
    now: datetime | None = None,
) -> ApplyResult:
    """Persist every non-skipped proposal as a vault Memory + index row.

    Skipped proposals (``summary`` empty) are counted but not
    written. Per-proposal failures are caught and reported in
    ``apply_errors`` so a single bad source session doesn't abort the
    whole sweep.

    Returns an :class:`ApplyResult` summarizing the apply outcome.
    """
    result = ApplyResult()
    for proposal in plan.proposals:
        if not proposal.summary:
            result.skipped_no_summary += 1
            continue
        candidate = proposal.candidate
        try:
            existing = _existing_distillation_for_session(vault, candidate.session_id)
            memory_id = str(existing.id) if existing is not None else None
            memory = materialize_distillation(
                candidate,
                proposal.summary,
                proposal.summarizer_name,
                now=now,
                memory_id=memory_id,
            )
            vault.write(memory)
            index.upsert(memory)
            result.written += 1
        except Exception as exc:
            err = f"distillation apply failed for session {candidate.session_id}: {exc}"
            logger.warning(err)
            result.apply_errors.append(err)
    return result


# ─── Reporting helpers ────────────────────────────────────────────


def format_plan_summary(plan: DistillationPlan) -> str:
    """One-paragraph human-readable summary of a plan.

    Used by the CLI ``--dry-run`` output. The per-proposal listing is
    handled by the CLI directly; this function returns the totals.
    """
    proposals = plan.proposals
    skipped_empty = sum(1 for p in proposals if not p.summary)
    summarized = len(proposals) - skipped_empty
    lines = [
        f"  scanned: {plan.total_sessions_scanned} session record(s)",
        f"  skipped (already distilled): {plan.skipped_already_distilled}",
        f"  skipped (too short): {plan.skipped_too_short}",
        f"  proposed: {summarized}",
        f"  skipped (summarizer empty): {skipped_empty}",
    ]
    return "\n".join(lines)


def format_proposals(plan: DistillationPlan, *, max_preview: int = 80) -> Iterable[str]:
    """Yield one short line per proposal for ``--dry-run`` output."""
    for proposal in plan.proposals:
        candidate = proposal.candidate
        marker = "✓" if proposal.summary else "·"
        head = (proposal.summary or proposal.skipped_reason or "").splitlines()
        preview = head[0] if head else ""
        if len(preview) > max_preview:
            preview = preview[: max_preview - 1] + "…"
        agent_part = f"/{candidate.agent}" if candidate.agent else ""
        yield (
            f"  {marker} {candidate.source}{agent_part}/{candidate.session_id}  "
            f"{candidate.turn_count} turns, {candidate.word_count} words  "
            f"— {preview}"
        )


__all__ = [
    "DEFAULT_DISTILLATION_IMPORTANCE",
    "DEFAULT_MIN_TURNS",
    "DEFAULT_MIN_WORDS",
    "DEFAULT_RECENCY_DAYS",
    "DISTILLATION_KIND_TAG",
    "PROVENANCE_REF_PREFIX",
    "ApplyResult",
    "DistillationPlan",
    "DistillationProposal",
    "SessionCandidate",
    "apply_distillations",
    "build_session_prompt",
    "compute_distillation_plan",
    "find_distilled_session_ids",
    "find_session_candidates",
    "format_plan_summary",
    "format_proposals",
    "is_meaningful_session",
    "materialize_distillation",
]
