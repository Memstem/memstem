"""In-daemon hygiene loop (ADR 0023).

Runs the four hygiene stages on configurable intervals as a background
``asyncio`` task inside ``memstem daemon``:

- :data:`~memstem.hygiene.state.STAGE_IMPORTANCE`
- :data:`~memstem.hygiene.state.STAGE_DISTILL_SESSIONS`
- :data:`~memstem.hygiene.state.STAGE_PROJECT_RECORDS`

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

logger = logging.getLogger(__name__)


class HygieneLoop:
    """The in-daemon hygiene runner.

    Constructed once per daemon run, owns its own summarizer handle
    (lazily built — instantiating an OpenAI client on a vault that
    won't actually need it for hours is wasteful).
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
        self._summarizer_unavailable_reason: str | None = None

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
            "hygiene loop: starting (poll=%ds, distill=%ds, "
            "importance=%ds, project_records=%ds, summarizer=%s)",
            self.cfg.loop_poll_interval_seconds,
            self.cfg.distill_interval_seconds,
            self.cfg.importance_interval_seconds,
            self.cfg.project_records_interval_seconds,
            self.cfg.summarizer_provider,
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
        lock = self.index.lock
        try:
            if not due_for_run(db, stage, interval_seconds, lock=lock):
                return
            if not acquire_stage_lock(
                db,
                stage,
                max_age_seconds=self.cfg.stage_lock_max_age_seconds,
                lock=lock,
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
            set_last_run(db, stage, datetime.now(UTC), lock=lock)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            logger.info("hygiene[%s]: cycle complete (%.1fs)", stage, elapsed)
        except asyncio.CancelledError:
            # Surface cancellation to the outer run() but release the lock first.
            try:
                release_stage_lock(db, stage, lock=lock)
            except Exception:
                logger.exception("hygiene[%s]: failed to release lock on cancel", stage)
            raise
        except Exception:
            logger.exception("hygiene[%s]: cycle failed", stage)
        finally:
            try:
                release_stage_lock(db, stage, lock=lock)
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
                # Build kwargs so callers who don't set base_url get
                # the OpenAISummarizer default (https://api.openai.com/v1)
                # rather than the explicit ``None`` overriding it.
                openai_kwargs: dict[str, object] = {
                    "model": self.cfg.summarizer_model or DEFAULT_OPENAI_MODEL,
                    "api_key_env": self.cfg.summarizer_api_key_env,
                }
                if self.cfg.summarizer_base_url:
                    openai_kwargs["base_url"] = self.cfg.summarizer_base_url
                self._summarizer = OpenAISummarizer(**openai_kwargs)  # type: ignore[arg-type]
            elif provider == "ollama":
                ollama_kwargs: dict[str, object] = {
                    "model": self.cfg.summarizer_model or DEFAULT_OLLAMA_MODEL,
                }
                if self.cfg.summarizer_base_url:
                    ollama_kwargs["base_url"] = self.cfg.summarizer_base_url
                self._summarizer = OllamaSummarizer(**ollama_kwargs)  # type: ignore[arg-type]
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

        # The cap is passed *into* the planner so the truncation happens
        # before the LLM calls — otherwise compute_distillation_plan
        # would call the summarizer on every eligible session and only
        # then we'd discard the overflow. On a cold vault that's
        # thousands of unnecessary calls (see ADR 0023 §Configuration).
        plan = compute_distillation_plan(
            self.vault,
            summarizer,
            db=self.index.db,
            max_candidates=self.cfg.distill_max_per_cycle,
            lock=self.index.lock,
        )
        if plan.skipped_failed:
            logger.info(
                "hygiene[distill_sessions]: skipping %d session(s) that exceeded the "
                "distill-retry cap",
                plan.skipped_failed,
            )
        if not plan.proposals:
            logger.info("hygiene[distill_sessions]: no eligible sessions")
            return

        result = apply_distillations(self.vault, self.index, plan, lock=self.index.lock)
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


__all__ = ["ALL_STAGES", "HygieneLoop"]
