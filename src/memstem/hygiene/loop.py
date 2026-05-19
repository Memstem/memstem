"""In-daemon hygiene loop (ADR 0023).

Runs the four hygiene stages on configurable intervals as a background
``asyncio`` task inside ``memstem daemon``:

- :data:`~memstem.hygiene.state.STAGE_IMPORTANCE`
- :data:`~memstem.hygiene.state.STAGE_DISTILL_SESSIONS`
- :data:`~memstem.hygiene.state.STAGE_PROJECT_RECORDS`
- :data:`~memstem.hygiene.state.STAGE_DEDUP_JUDGE`

Each stage:

1. Checks its interval via ``hygiene_state.last_run:<stage>``.
2. Acquires a per-stage lock (``hygiene_state.running_since:<stage>``).
   The same lock is checked by the CLI hygiene commands so manual runs
   and the loop never compete.
3. Runs the underlying planner + applier via :func:`asyncio.to_thread`
   (the hygiene modules are synchronous).
4. Records ``last_run:<stage>`` on success, releases the lock either way.

A stage that raises does not affect other stages or the wider daemon —
the exception is logged and the loop continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from memstem.hygiene.state import (
    ALL_STAGES,
    STAGE_DEDUP_JUDGE,
    STAGE_DISTILL_SESSIONS,
    STAGE_IMPORTANCE,
    STAGE_PROJECT_RECORDS,
    acquire_stage_lock,
    due_for_run,
    release_stage_lock,
    set_last_run,
)

if TYPE_CHECKING:
    from memstem.config import HygieneConfig
    from memstem.core.index import Index
    from memstem.core.storage import Vault
    from memstem.core.summarizer import Summarizer
    from memstem.hygiene.dedup_judge import DedupJudge

logger = logging.getLogger(__name__)


class HygieneLoop:
    """The in-daemon hygiene runner.

    Constructed once per daemon run, owns its own summarizer / judge
    handles (lazily built — instantiating an OpenAI client on a vault
    that won't actually need it for hours is wasteful).
    """

    def __init__(
        self,
        vault: Vault,
        index: Index,
        cfg: HygieneConfig,
    ) -> None:
        self.vault = vault
        self.index = index
        self.cfg = cfg
        self._summarizer: Summarizer | None = None
        self._judge: DedupJudge | None = None
        self._summarizer_unavailable_reason: str | None = None
        self._judge_unavailable_reason: str | None = None

    # -- Public entry --------------------------------------------------

    async def run(self) -> None:
        """Main loop. Polls until cancelled."""
        if not self.cfg.loop_enabled:
            logger.info(
                "hygiene loop: disabled (config.hygiene.loop_enabled=false); "
                "CLI hygiene commands still work"
            )
            return

        logger.info(
            "hygiene loop: starting (poll=%ds, distill=%ds, dedup=%ds, "
            "importance=%ds, project_records=%ds, summarizer=%s, judge=%s)",
            self.cfg.loop_poll_interval_seconds,
            self.cfg.distill_interval_seconds,
            self.cfg.dedup_interval_seconds,
            self.cfg.importance_interval_seconds,
            self.cfg.project_records_interval_seconds,
            self.cfg.summarizer_provider,
            self.cfg.judge_provider,
        )

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                logger.info("hygiene loop: cancelled")
                raise
            except Exception:
                # Belt-and-suspenders: _tick itself isolates per-stage
                # exceptions, but if something throws between stages
                # (e.g. sqlite connection issue) we still want the
                # loop to survive.
                logger.exception("hygiene loop: tick failed")
            await asyncio.sleep(self.cfg.loop_poll_interval_seconds)

    # -- Tick orchestration --------------------------------------------

    async def _tick(self) -> None:
        """One pass: check every stage, run any that are due."""
        stages: list[tuple[str, int, Callable[[], None]]] = [
            (STAGE_IMPORTANCE, self.cfg.importance_interval_seconds, self._run_importance),
            (
                STAGE_DISTILL_SESSIONS,
                self.cfg.distill_interval_seconds,
                self._run_distill_sessions,
            ),
            (
                STAGE_PROJECT_RECORDS,
                self.cfg.project_records_interval_seconds,
                self._run_project_records,
            ),
            (STAGE_DEDUP_JUDGE, self.cfg.dedup_interval_seconds, self._run_dedup_judge),
        ]
        for stage, interval, fn in stages:
            await self._maybe_run_stage(stage, interval, fn)

    async def _maybe_run_stage(
        self,
        stage: str,
        interval_seconds: int,
        fn: Callable[[], None],
    ) -> None:
        db = self.index.db
        try:
            if not due_for_run(db, stage, interval_seconds):
                return
            if not acquire_stage_lock(
                db,
                stage,
                max_age_seconds=self.cfg.stage_lock_max_age_seconds,
            ):
                logger.debug("hygiene[%s]: lock held by another runner", stage)
                return
        except Exception:
            logger.exception("hygiene[%s]: failed to schedule", stage)
            return

        started = datetime.now(UTC)
        logger.info("hygiene[%s]: starting cycle", stage)
        try:
            await asyncio.to_thread(fn)
            set_last_run(db, stage, datetime.now(UTC))
            elapsed = (datetime.now(UTC) - started).total_seconds()
            logger.info("hygiene[%s]: cycle complete (%.1fs)", stage, elapsed)
        except asyncio.CancelledError:
            # Surface cancellation to the outer run() but release the lock first.
            try:
                release_stage_lock(db, stage)
            except Exception:
                logger.exception("hygiene[%s]: failed to release lock on cancel", stage)
            raise
        except Exception:
            logger.exception("hygiene[%s]: cycle failed", stage)
        finally:
            try:
                release_stage_lock(db, stage)
            except Exception:
                logger.exception("hygiene[%s]: failed to release lock", stage)

    # -- Lazy helpers --------------------------------------------------

    def _get_summarizer(self) -> Summarizer | None:
        if self._summarizer is not None or self._summarizer_unavailable_reason is not None:
            return self._summarizer
        from memstem.core.summarizer import (
            DEFAULT_OLLAMA_MODEL,
            DEFAULT_OPENAI_MODEL,
            NoOpSummarizer,
            OllamaSummarizer,
            OpenAISummarizer,
        )

        provider = self.cfg.summarizer_provider.lower()
        try:
            if provider == "noop":
                self._summarizer = NoOpSummarizer()
            elif provider == "openai":
                self._summarizer = OpenAISummarizer(
                    model=self.cfg.summarizer_model or DEFAULT_OPENAI_MODEL,
                )
            elif provider == "ollama":
                self._summarizer = OllamaSummarizer(
                    model=self.cfg.summarizer_model or DEFAULT_OLLAMA_MODEL,
                )
            else:
                self._summarizer_unavailable_reason = (
                    f"unknown summarizer provider {self.cfg.summarizer_provider!r}; "
                    "expected one of: noop, openai, ollama"
                )
                logger.warning("hygiene loop: %s", self._summarizer_unavailable_reason)
        except Exception as exc:
            self._summarizer_unavailable_reason = (
                f"summarizer init failed ({type(exc).__name__}: {exc})"
            )
            logger.warning("hygiene loop: %s", self._summarizer_unavailable_reason)
        return self._summarizer

    def _get_judge(self) -> DedupJudge | None:
        if self._judge is not None or self._judge_unavailable_reason is not None:
            return self._judge
        from memstem.hygiene.dedup_judge import NoOpJudge, OllamaDedupJudge

        provider = self.cfg.judge_provider.lower()
        try:
            if provider == "noop":
                self._judge = NoOpJudge()
            elif provider == "ollama":
                self._judge = OllamaDedupJudge()
            else:
                self._judge_unavailable_reason = (
                    f"unknown judge provider {self.cfg.judge_provider!r}; "
                    "expected one of: noop, ollama"
                )
                logger.warning("hygiene loop: %s", self._judge_unavailable_reason)
        except Exception as exc:
            self._judge_unavailable_reason = f"judge init failed ({type(exc).__name__}: {exc})"
            logger.warning("hygiene loop: %s", self._judge_unavailable_reason)
        return self._judge

    # -- Stage runners -------------------------------------------------
    #
    # These are called via asyncio.to_thread — synchronous, may block on
    # SQLite / LLM IO, no asyncio primitives inside.

    def _run_importance(self) -> None:
        from memstem.hygiene.importance import (
            apply_importance_updates,
            compute_importance_updates,
        )

        plan = compute_importance_updates(self.vault, self.index)
        if not plan.updates:
            # Still advance the cursor so the next sweep starts from a
            # fresh window — otherwise an empty vault re-scans the
            # same stale tail forever.
            apply_importance_updates(self.vault, self.index, plan)
            logger.info("hygiene[importance]: no bumps proposed; cursor advanced")
            return
        n = apply_importance_updates(self.vault, self.index, plan)
        logger.info("hygiene[importance]: applied %d bump(s)", n)

    def _run_distill_sessions(self) -> None:
        from memstem.hygiene.session_distill import (
            apply_distillations,
            compute_distillation_plan,
        )

        summarizer = self._get_summarizer()
        if summarizer is None:
            logger.info("hygiene[distill_sessions]: skipped — summarizer unavailable")
            return

        plan = compute_distillation_plan(
            self.vault,
            summarizer,
            db=self.index.db,
        )
        if not plan.proposals:
            logger.info("hygiene[distill_sessions]: no eligible sessions")
            return

        # Apply at most distill_max_per_cycle to keep cycles bounded.
        # The plan dataclass is frozen but proposals is a mutable list,
        # so we trim in place rather than constructing a new plan.
        if len(plan.proposals) > self.cfg.distill_max_per_cycle:
            logger.info(
                "hygiene[distill_sessions]: capping %d proposals to %d this cycle",
                len(plan.proposals),
                self.cfg.distill_max_per_cycle,
            )
            del plan.proposals[self.cfg.distill_max_per_cycle :]

        result = apply_distillations(self.vault, self.index, plan)
        logger.info(
            "hygiene[distill_sessions]: wrote %d, skipped_no_summary=%d, errors=%d",
            result.written,
            result.skipped_no_summary,
            len(result.apply_errors),
        )
        for err in result.apply_errors:
            logger.warning("hygiene[distill_sessions]: apply error: %s", err)

    def _run_project_records(self) -> None:
        from memstem.hygiene.project_records import (
            apply_project_records,
            compute_project_record_plan,
        )

        summarizer = self._get_summarizer()
        if summarizer is None:
            logger.info("hygiene[project_records]: skipped — summarizer unavailable")
            return

        plan = compute_project_record_plan(self.vault, summarizer, db=self.index.db)
        if not plan.proposals:
            logger.info("hygiene[project_records]: no eligible projects")
            return
        result = apply_project_records(self.vault, self.index, plan)
        logger.info(
            "hygiene[project_records]: wrote %d, updated %d, skipped_no_summary=%d, errors=%d",
            result.written,
            result.updated,
            result.skipped_no_summary,
            len(result.apply_errors),
        )
        for err in result.apply_errors:
            logger.warning("hygiene[project_records]: apply error: %s", err)

    def _run_dedup_judge(self) -> None:
        from memstem.hygiene.dedup_candidates import find_dedup_candidate_pairs
        from memstem.hygiene.dedup_judge import judge_pairs, write_audit_rows

        judge = self._get_judge()
        if judge is None:
            logger.info("hygiene[dedup_judge]: skipped — judge unavailable")
            return

        pairs = find_dedup_candidate_pairs(
            self.vault,
            self.index,
            min_cosine=self.cfg.dedup_threshold,
        )
        if not pairs:
            logger.info("hygiene[dedup_judge]: no candidate pairs")
            return

        if len(pairs) > self.cfg.dedup_max_per_cycle:
            logger.info(
                "hygiene[dedup_judge]: capping %d pairs to %d this cycle",
                len(pairs),
                self.cfg.dedup_max_per_cycle,
            )
            pairs = pairs[: self.cfg.dedup_max_per_cycle]

        results = judge_pairs(pairs, judge=judge)
        n_written = write_audit_rows(self.index.db, results)
        logger.info("hygiene[dedup_judge]: wrote %d audit row(s)", n_written)


__all__ = ["ALL_STAGES", "HygieneLoop"]
