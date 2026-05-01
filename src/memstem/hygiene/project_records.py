"""Project records writer (ADR 0021).

Aggregates the per-Claude-Code-project-tag sessions in the vault into
a single ``type: project`` record at
``vault/memories/projects/<slug>.md``. The record is the canonical
retrieval-shaped representation of an ongoing piece of work, and it
accumulates as new sessions land for the same tag.

Mirrors the pattern from :mod:`memstem.hygiene.session_distill`:

1. :func:`find_project_candidates` walks the vault, groups
   ``type: session`` records by their Claude Code project tag, and
   returns one candidate per tag with at least
   :data:`DEFAULT_MIN_SESSIONS` sessions.
2. :func:`compute_project_record_plan` builds a prompt per candidate
   (preferring linked distillations over raw session bodies), runs it
   through the configured :class:`Summarizer`, and returns a plan the
   CLI can preview.
3. :func:`apply_project_records` writes / updates the project record
   markdown file and upserts the index. Records flagged
   ``manual: true`` get their links and ``updated`` refreshed but
   keep their hand-edited body — :data:`FORCE_OVERRIDES_MANUAL` lets
   ``--force`` override that.

Design notes (per ADR 0021):

- **Project = Claude Code project tag with ≥ 2 sessions.** OpenClaw
  memories don't carry an equivalent free signal in v1.
- **Slug = the tag itself.** Path is
  ``memories/projects/<slug>.md``; stable across runs so re-runs hit
  the same file.
- **Body source preference:** if a session has a linked distillation,
  use the distillation; otherwise fall back to the raw session body.
  Distillation-first produces cleaner project records because
  per-session noise is already filtered.
- **Importance seed = 0.85**, slightly above session distillations
  (0.8), so a project record outranks its constituent distillations
  on close ties.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from memstem.core.frontmatter import MemoryType, validate
from memstem.core.index import Index
from memstem.core.storage import Memory, Vault
from memstem.core.summarizer import Summarizer

logger = logging.getLogger(__name__)


DEFAULT_MIN_SESSIONS = 2
"""ADR 0021 §What is a project: a tag with at least this many session
records is treated as a project. Below the threshold, the session
distillation is enough on its own."""

DEFAULT_PROJECT_IMPORTANCE = 0.85
"""ADR 0021 §Search ranking: project record seeds get this importance
value — slightly above session distillations (0.8) so a project record
outranks any specific session-shaped result for the same project on
close ties."""

DEFAULT_MAX_INPUT_CHARS = 12000
"""Cap on combined source-input length sent to the project summarizer.

Brad's Woodfield project today is ~5 sessions of mixed length. As
projects grow we don't want to blow past the LLM's context window.
This cap is per-prompt; the prompt template includes a continuation
marker so the LLM knows when input was truncated. 12k chars ≈ 3k
tokens of source context — enough for a quality project rollup, well
under any modern model's context window."""

PROVENANCE_REF_PREFIX = "project:"
"""Prefix on ``frontmatter.provenance.ref`` for project records.
Combined with the slug it forms a stable id like ``project:home-ubuntu-
woodfield-quotes`` so the writer can recognize its own outputs."""

PROJECT_KIND_TAG = "project:claude-code"
"""Static tag added to every project record. Lets agents filter
``types=[project]`` and additionally identify the source signal
(claude-code tag) the writer used to identify the project."""

MANUAL_FLAG_KEY = "manual"
"""Frontmatter key that, when set to ``True``, prevents the project
writer from regenerating the body. Used for hand-curated project
records the operator wants to protect from LLM clobbering. ``--force``
overrides this; see :data:`FORCE_OVERRIDES_MANUAL`."""

FORCE_OVERRIDES_MANUAL = True
"""ADR 0021 §Manual override: ``--force`` regenerates even
``manual: true`` records. The CLI surfaces a warning so the operator
notices."""

# Tag prefixes that aren't project tags. Sessions with only these
# tags are skipped by the project-candidate scan.
_NON_PROJECT_TAG_PREFIXES = (
    "agent:",
    "topic:",
    "distillation:",
    "project:",
)
_NON_PROJECT_TAG_LITERALS = frozenset({"instructions", "core", "shared"})


# ─── Data classes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectCandidate:
    """One project: the slug + its source sessions and distillations.

    ``sessions`` is non-empty (filtered by the threshold).
    ``distillations`` may be a strict subset — sessions without a
    linked distillation contribute their raw body to the prompt.
    Both lists are sorted by ``updated`` ascending so the prompt
    feeds chronological order.
    """

    slug: str
    sessions: list[Memory]
    distillations: list[Memory]
    earliest_created: datetime
    latest_updated: datetime

    @property
    def session_count(self) -> int:
        return len(self.sessions)


@dataclass(frozen=True)
class ProjectProposal:
    """One planned project record write or update.

    ``is_update`` is ``True`` when an existing project record is being
    refreshed; ``False`` for a brand-new record. ``manual_skip`` is
    ``True`` when the existing record carries ``manual: true`` and
    ``--force`` was not set; in that case the writer updates ``links``
    and ``updated`` but preserves the body.

    ``existing_memory_id`` is the id of the existing record (when
    updating) so the writer can preserve it for in-place overwrite.
    """

    candidate: ProjectCandidate
    body: str
    summarizer_name: str
    is_update: bool
    manual_skip: bool
    existing_memory_id: str | None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class ProjectRecordPlan:
    """Full sweep result, returned by :func:`compute_project_record_plan`."""

    proposals: list[ProjectProposal] = field(default_factory=list)
    total_tags_scanned: int = 0
    skipped_below_threshold: int = 0


@dataclass
class ApplyResult:
    """Side-effect summary, returned by :func:`apply_project_records`."""

    written: int = 0
    updated: int = 0
    links_only_updates: int = 0
    skipped_no_summary: int = 0
    apply_errors: list[str] = field(default_factory=list)


# ─── Discovery helpers ────────────────────────────────────────────


def _is_project_tag(tag: str) -> bool:
    if tag in _NON_PROJECT_TAG_LITERALS:
        return False
    return not tag.startswith(_NON_PROJECT_TAG_PREFIXES)


def project_tag_for_session(memory: Memory) -> str | None:
    """Return the Claude Code project tag from a session, or ``None``.

    A session's tags include the encoded project directory (e.g.
    ``home-ubuntu-woodfield-quotes`` for sessions in
    ``~/.claude/projects/-home-ubuntu-woodfield-quotes/``). Other tag
    shapes (``agent:*``, ``topic:*``, ``distillation:*``,
    ``project:*``, plus the literal ``instructions`` / ``core`` /
    ``shared``) are not project tags.

    Returns the first project-tag-shaped value, or ``None`` when no
    such tag is present (e.g. OpenClaw trajectory sessions, which
    only carry ``agent:<tag>``).
    """
    if memory.type is not MemoryType.SESSION:
        return None
    for tag in memory.frontmatter.tags:
        if _is_project_tag(tag):
            return tag
    return None


def _session_id_from_link(link: str) -> str | None:
    if not link:
        return None
    stripped = link.strip()
    for prefix in ("memory://sessions/", "sessions/"):
        if stripped.startswith(prefix):
            tail = stripped[len(prefix) :]
            if tail.endswith(".md"):
                tail = tail[: -len(".md")]
            return tail or None
    return None


def _build_session_distillation_index(vault: Vault) -> dict[str, Memory]:
    """Map ``session_id -> distillation Memory`` for fast lookup.

    Walks every ``type: distillation`` record once. Sessions without a
    linked distillation are absent from the map.
    """
    index: dict[str, Memory] = {}
    for memory in vault.walk(types=[MemoryType.DISTILLATION.value]):
        for link in memory.frontmatter.links:
            session_id = _session_id_from_link(link)
            if session_id:
                index[session_id] = memory
    return index


def find_project_candidates(
    vault: Vault,
    *,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
) -> tuple[list[ProjectCandidate], dict[str, int]]:
    """Group sessions by project tag; return one candidate per qualifying tag.

    Returns ``(candidates, stats)`` where ``stats`` carries:
    - ``total_tags_scanned``: distinct project tags seen.
    - ``skipped_below_threshold``: tags with fewer than
      ``min_sessions`` sessions.
    """
    by_tag: dict[str, list[Memory]] = defaultdict(list)
    for memory in vault.walk(types=[MemoryType.SESSION.value]):
        tag = project_tag_for_session(memory)
        if tag is None:
            continue
        by_tag[tag].append(memory)

    distill_by_session = _build_session_distillation_index(vault)

    candidates: list[ProjectCandidate] = []
    skipped = 0
    for tag, sessions in by_tag.items():
        if len(sessions) < min_sessions:
            skipped += 1
            continue
        sessions_sorted = sorted(sessions, key=lambda m: m.frontmatter.updated)
        distillations: list[Memory] = []
        seen_distill_ids: set[str] = set()
        for session in sessions_sorted:
            session_id = Path(session.path).stem
            distill = distill_by_session.get(session_id)
            if distill is not None and str(distill.id) not in seen_distill_ids:
                distillations.append(distill)
                seen_distill_ids.add(str(distill.id))
        candidates.append(
            ProjectCandidate(
                slug=tag,
                sessions=sessions_sorted,
                distillations=distillations,
                earliest_created=min(s.frontmatter.created for s in sessions_sorted),
                latest_updated=max(s.frontmatter.updated for s in sessions_sorted),
            )
        )

    candidates.sort(key=lambda c: c.slug)
    stats = {
        "total_tags_scanned": len(by_tag),
        "skipped_below_threshold": skipped,
    }
    return candidates, stats


# ─── Prompt construction ──────────────────────────────────────────


def _load_project_prompt() -> str:
    """Read the canonical project-record prompt template."""
    path = Path(__file__).parent.parent / "prompts" / "distill_project.txt"
    return path.read_text(encoding="utf-8")


def _truncate_with_marker(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    remaining = len(text) - max_chars
    return f"{head}\n\n[…input continues for {remaining:,} more chars]"


def _format_source_section(memory: Memory, *, kind: str) -> str:
    """Render one source memory as a section in the project prompt."""
    title = memory.frontmatter.title or "(untitled)"
    body = memory.body or ""
    when = memory.frontmatter.updated.date().isoformat()
    return f"### [{kind}] {when} — {title}\n\n{body}"


def build_project_prompt(
    candidate: ProjectCandidate,
    *,
    prompt_template: str | None = None,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> str:
    """Render the project-record prompt for one candidate.

    Sources are concatenated in chronological order. Each source is
    prefixed with a kind label (``distillation`` or ``session``) so
    the LLM knows which it's reading. When a session has a linked
    distillation, the distillation is used instead of the raw body
    (cleaner input → cleaner output).
    """
    template = prompt_template or _load_project_prompt()

    # Build {session_id -> distillation} for quick "does this session
    # have a distillation?" checks.
    distill_by_session: dict[str, Memory] = {}
    for d in candidate.distillations:
        for link in d.frontmatter.links:
            session_id = _session_id_from_link(link)
            if session_id:
                distill_by_session[session_id] = d

    # Compose the source sections in chronological order. Prefer the
    # distillation when available; fall back to the raw session body.
    pieces: list[str] = []
    distill_count = 0
    raw_count = 0
    for session in candidate.sessions:
        session_id = Path(session.path).stem
        distill = distill_by_session.get(session_id)
        if distill is not None:
            pieces.append(_format_source_section(distill, kind="distillation"))
            distill_count += 1
        else:
            pieces.append(_format_source_section(session, kind="session"))
            raw_count += 1
    sources_text = "\n\n".join(pieces)
    sources_text = _truncate_with_marker(sources_text, max_input_chars)

    if distill_count and raw_count:
        input_type = f"mixed ({distill_count} distillation, {raw_count} raw session)"
    elif distill_count:
        input_type = f"{distill_count} distillation"
    else:
        input_type = f"{raw_count} raw session"

    return template.format(
        tag=candidate.slug,
        session_count=candidate.session_count,
        input_type=input_type,
        sources=sources_text,
    )


# ─── Existing record + manual flag ────────────────────────────────


def _project_path(slug: str) -> Path:
    return Path("memories", "projects", f"{slug}.md")


def is_manual(memory: Memory) -> bool:
    """Return True iff the project record carries ``manual: true``.

    The flag rides on Pydantic's ``extra="allow"`` so it round-trips
    without a schema change. Any truthy value is treated as set.
    """
    extra = getattr(memory.frontmatter, "model_extra", None) or {}
    raw = extra.get(MANUAL_FLAG_KEY)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.lower() in {"true", "yes", "1"}
    return False


def existing_project_record(vault: Vault, slug: str) -> Memory | None:
    """Read the existing project record for ``slug`` if any.

    Returns ``None`` when no file exists at the canonical path.
    """
    candidate_path = _project_path(slug)
    try:
        return vault.read(candidate_path)
    except Exception:
        # MemoryNotFoundError or other read failure → treat as absent.
        return None


# ─── Materialization ──────────────────────────────────────────────


def _project_links(candidate: ProjectCandidate) -> list[str]:
    links: list[str] = []
    for session in candidate.sessions:
        session_id = Path(session.path).stem
        links.append(f"memory://sessions/{session_id}")
    for distill in candidate.distillations:
        links.append(f"memory://memories/distillations/{distill.id}")
    return links


def _project_tags(candidate: ProjectCandidate) -> list[str]:
    tags = [candidate.slug]
    if PROJECT_KIND_TAG not in tags:
        tags.append(PROJECT_KIND_TAG)
    return tags


def materialize_project_record(
    candidate: ProjectCandidate,
    body: str,
    summarizer_name: str,
    *,
    existing: Memory | None = None,
    importance: float = DEFAULT_PROJECT_IMPORTANCE,
    now: datetime | None = None,
    preserve_manual_body: bool = False,
) -> Memory:
    """Build the ``type: project`` :class:`Memory` for a candidate.

    When ``existing`` is provided, the new memory reuses its
    ``id``, ``created`` timestamp, and (when
    ``preserve_manual_body=True``) its ``body``. ``preserve_manual_body``
    captures the ``manual: true`` semantics — body locked, but the
    writer still refreshes ``links`` and ``updated`` so the link map
    stays current.
    """
    timestamp = now or datetime.now(tz=UTC)
    new_id = str(existing.id) if existing is not None else str(uuid4())
    created = existing.frontmatter.created if existing is not None else candidate.earliest_created

    # Honor the manual override on the body if requested.
    final_body = body
    manual_flag = False
    if existing is not None and preserve_manual_body:
        final_body = existing.body
        manual_flag = True

    payload: dict[str, Any] = {
        "id": new_id,
        "type": MemoryType.PROJECT.value,
        "created": created.isoformat(),
        "updated": timestamp.isoformat(),
        "source": "hygiene-worker",
        "title": _extract_title_from_body(final_body) or candidate.slug,
        "tags": _project_tags(candidate),
        "links": _project_links(candidate),
        "importance": importance,
        "provenance": {
            "source": "hygiene-worker",
            "ref": f"{PROVENANCE_REF_PREFIX}{candidate.slug}",
            "ingested_at": timestamp.isoformat(),
            "summarizer": summarizer_name,
        },
    }
    if manual_flag:
        payload[MANUAL_FLAG_KEY] = True
    fm = validate(payload)
    return Memory(frontmatter=fm, body=final_body, path=_project_path(candidate.slug))


def _extract_title_from_body(body: str) -> str | None:
    """Pull the leading ``# Title`` line from the LLM body, if present.

    The project prompt asks for ``# <Canonical project title>`` as
    line 1. Extract it for the frontmatter ``title`` field so the
    title is consistent with the body header.
    """
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
        # Stop at the first non-blank, non-H1 line — the prompt
        # contract is "first line is the title."
        return None
    return None


# ─── Plan + apply ─────────────────────────────────────────────────


def compute_project_record_plan(
    vault: Vault,
    summarizer: Summarizer,
    *,
    db: object | None = None,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    force: bool = False,
    prompt_template: str | None = None,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    now: datetime | None = None,
) -> ProjectRecordPlan:
    """Build the full project-record plan against the configured summarizer.

    For each project candidate, the writer:

    1. Reads any existing project record at
       ``memories/projects/<slug>.md``.
    2. If ``manual: true`` and ``force=False``, marks the proposal
       ``manual_skip=True`` and bypasses the LLM call entirely (the
       applier will refresh links + ``updated`` only).
    3. Otherwise calls :meth:`Summarizer.generate_cached` with the
       candidate's prompt; cache hits short-circuit.

    ``force=True`` regenerates everything, including ``manual: true``
    records (per :data:`FORCE_OVERRIDES_MANUAL`).
    """
    candidates, stats = find_project_candidates(vault, min_sessions=min_sessions)
    proposals: list[ProjectProposal] = []
    for candidate in candidates:
        existing = existing_project_record(vault, candidate.slug)
        manual = existing is not None and is_manual(existing)
        manual_skip = manual and not (force and FORCE_OVERRIDES_MANUAL)

        if manual_skip:
            proposals.append(
                ProjectProposal(
                    candidate=candidate,
                    body=existing.body if existing is not None else "",
                    summarizer_name=summarizer.name,
                    is_update=True,
                    manual_skip=True,
                    existing_memory_id=str(existing.id) if existing is not None else None,
                    skipped_reason="manual:true preserves body (use --force to override)",
                )
            )
            continue

        prompt = build_project_prompt(
            candidate,
            prompt_template=prompt_template,
            max_input_chars=max_input_chars,
        )
        body = summarizer.generate_cached(prompt, db=db)  # type: ignore[arg-type]
        skipped_reason: str | None = None
        if not body:
            skipped_reason = "summarizer returned empty (NoOp default or LLM unreachable)"
        proposals.append(
            ProjectProposal(
                candidate=candidate,
                body=body,
                summarizer_name=summarizer.name,
                is_update=existing is not None,
                manual_skip=False,
                existing_memory_id=str(existing.id) if existing is not None else None,
                skipped_reason=skipped_reason,
            )
        )

    return ProjectRecordPlan(
        proposals=proposals,
        total_tags_scanned=stats["total_tags_scanned"],
        skipped_below_threshold=stats["skipped_below_threshold"],
    )


def apply_project_records(
    vault: Vault,
    index: Index,
    plan: ProjectRecordPlan,
    *,
    now: datetime | None = None,
) -> ApplyResult:
    """Persist every non-skipped proposal as a vault record + index row.

    ``manual_skip`` proposals refresh links and ``updated`` only — the
    body is preserved. Empty-body proposals (NoOp / LLM unreachable
    on a non-manual record) are counted but not written.

    Per-proposal failures are captured in ``apply_errors`` so a single
    bad project doesn't abort the sweep.
    """
    result = ApplyResult()
    for proposal in plan.proposals:
        if not proposal.body and not proposal.manual_skip:
            result.skipped_no_summary += 1
            continue
        candidate = proposal.candidate
        try:
            existing = existing_project_record(vault, candidate.slug)
            memory = materialize_project_record(
                candidate,
                proposal.body,
                proposal.summarizer_name,
                existing=existing,
                now=now,
                preserve_manual_body=proposal.manual_skip,
            )
            vault.write(memory)
            index.upsert(memory)
            # Enqueue for embedding so vec retrieval can rank the new
            # project record. Same rationale as the session-distill
            # writer: a focused project summary loses to sprawling
            # source transcripts on BM25 term-frequency alone.
            index.enqueue_embed(str(memory.id))
            if proposal.manual_skip:
                result.links_only_updates += 1
            elif proposal.is_update:
                result.updated += 1
            else:
                result.written += 1
        except Exception as exc:
            err = f"project record apply failed for slug {candidate.slug}: {exc}"
            logger.warning(err)
            result.apply_errors.append(err)
    return result


# ─── Reporting helpers ────────────────────────────────────────────


def format_plan_summary(plan: ProjectRecordPlan) -> str:
    proposals = plan.proposals
    summarized = sum(1 for p in proposals if p.body and not p.manual_skip)
    skipped_empty = sum(1 for p in proposals if not p.body and not p.manual_skip)
    manual_skips = sum(1 for p in proposals if p.manual_skip)
    new_records = sum(1 for p in proposals if not p.is_update and p.body)
    updates = sum(1 for p in proposals if p.is_update and not p.manual_skip and p.body)
    lines = [
        f"  scanned: {plan.total_tags_scanned} project tag(s)",
        f"  skipped (below {DEFAULT_MIN_SESSIONS}-session threshold): {plan.skipped_below_threshold}",
        f"  proposed (new records): {new_records}",
        f"  proposed (refresh): {updates}",
        f"  manual:true preserved (links only): {manual_skips}",
        f"  skipped (summarizer empty): {skipped_empty}",
        f"  total proposals: {len(proposals)} (summarized: {summarized})",
    ]
    return "\n".join(lines)


def format_proposals(plan: ProjectRecordPlan, *, max_preview: int = 80) -> Iterable[str]:
    """Yield one short line per proposal for ``--dry-run`` output."""
    for proposal in plan.proposals:
        candidate = proposal.candidate
        if proposal.manual_skip:
            marker = "M"  # manual-preserved
        elif proposal.body:
            marker = "✓"
        else:
            marker = "·"  # skipped
        head = (proposal.body or proposal.skipped_reason or "").splitlines()
        preview = head[0] if head else ""
        if len(preview) > max_preview:
            preview = preview[: max_preview - 1] + "…"
        verb = "update" if proposal.is_update else "create"
        yield (
            f"  {marker} [{verb}] {candidate.slug}  "
            f"({candidate.session_count} sessions, "
            f"{len(candidate.distillations)} distillations)  "
            f"— {preview}"
        )


__all__ = [
    "DEFAULT_MAX_INPUT_CHARS",
    "DEFAULT_MIN_SESSIONS",
    "DEFAULT_PROJECT_IMPORTANCE",
    "FORCE_OVERRIDES_MANUAL",
    "MANUAL_FLAG_KEY",
    "PROJECT_KIND_TAG",
    "PROVENANCE_REF_PREFIX",
    "ApplyResult",
    "ProjectCandidate",
    "ProjectProposal",
    "ProjectRecordPlan",
    "apply_project_records",
    "build_project_prompt",
    "compute_project_record_plan",
    "existing_project_record",
    "find_project_candidates",
    "format_plan_summary",
    "format_proposals",
    "is_manual",
    "materialize_project_record",
    "project_tag_for_session",
]
