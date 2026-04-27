"""Tests for the write-time noise filter (ADR 0011, PR-A)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memstem.adapters.base import MemoryRecord
from memstem.core.extraction import (
    NoiseAction,
    NoiseDecision,
    is_cron_output,
    is_heartbeat,
    is_tool_dump,
    noise_filter,
)
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault


def _record(
    body: str = "Some legitimate memory content about Brad's project.",
    *,
    source: str = "openclaw",
    ref: str = "/tmp/test.md",
) -> MemoryRecord:
    metadata: dict[str, Any] = {
        "type": "memory",
        "created": "2026-04-27T10:00:00+00:00",
        "updated": "2026-04-27T10:00:00+00:00",
    }
    return MemoryRecord(
        source=source,
        ref=ref,
        title="Test",
        body=body,
        tags=[],
        metadata=metadata,
    )


# --- Heartbeat detection ---


class TestIsHeartbeat:
    def test_exact_heartbeat_ok_marker(self) -> None:
        assert is_heartbeat("HEARTBEAT_OK") is True

    def test_heartbeat_ok_with_surrounding_whitespace(self) -> None:
        assert is_heartbeat("\n  HEARTBEAT_OK  \n") is True

    def test_heartbeat_marker_at_line_start(self) -> None:
        assert is_heartbeat("[heartbeat]\nstatus: ok\nlast: 2026-04-27") is True

    def test_heartbeat_marker_case_insensitive(self) -> None:
        assert is_heartbeat("[HEARTBEAT] running") is True

    def test_legitimate_prose_mentioning_heartbeat_is_not_dropped(self) -> None:
        # A skill or memory that *talks about* heartbeats must pass.
        body = (
            "The agent's heartbeat mechanism is documented in /docs/heartbeat.md. "
            "It pings every 60 seconds via PM2."
        )
        assert is_heartbeat(body) is False

    def test_skill_documentation_not_caught(self) -> None:
        body = """# Heartbeat skill

This skill describes how to interpret heartbeat signals from agents.
The heartbeat output is logged to /var/log/heartbeat.
"""
        assert is_heartbeat(body) is False

    def test_empty_body(self) -> None:
        assert is_heartbeat("") is False

    def test_whitespace_only_body(self) -> None:
        assert is_heartbeat("   \n\n  \t  ") is False


# --- Cron output detection ---


class TestIsCronOutput:
    def test_openclaw_dream_marker(self) -> None:
        body = "Triggered __openclaw_memory_core_short_term_promotion_dream__ at 04:00"
        assert is_cron_output(body) is True

    def test_openclaw_dream_marker_case_insensitive(self) -> None:
        assert is_cron_output("__OPENCLAW_TEST_DREAM__ ran") is True

    def test_running_cron_job_prefix(self) -> None:
        assert is_cron_output("Running cron job: backup\nfinished") is True

    def test_legitimate_prose_about_cron(self) -> None:
        body = "We use cron to schedule the nightly backup. See PLAN.md for the schedule format."
        assert is_cron_output(body) is False

    def test_documentation_about_dream_pipeline(self) -> None:
        # A document explaining OpenClaw's dream pipeline must pass through.
        body = "OpenClaw runs a nightly dream consolidation pass at 4am ET."
        assert is_cron_output(body) is False

    def test_empty_body(self) -> None:
        assert is_cron_output("") is False


# --- Tool dump detection ---


class TestIsToolDump:
    def test_short_body_below_threshold(self) -> None:
        # Short bodies are skipped; the heuristic is for long sessions.
        assert is_tool_dump('{"x": 1, "y": 2}') is False

    def test_mostly_json_lines_dump(self) -> None:
        body = "\n".join(
            [
                '{"action": "tool_use", "name": "Bash"}',
                '{"output": "result 1 with some long text to push past 200 chars"}',
                '{"output": "result 2 with some long text to push past 200 chars"}',
                '{"output": "result 3 with some long text to push past 200 chars"}',
                '{"output": "result 4 with some long text to push past 200 chars"}',
                '{"output": "result 5 with some long text to push past 200 chars"}',
                '{"output": "result 6 with some long text to push past 200 chars"}',
            ]
        )
        assert is_tool_dump(body) is True

    def test_mostly_tool_blocks_dump(self) -> None:
        body = "\n".join(
            [
                "[tool_use: Bash] running command number one across many lines",
                "[tool_result] /var/log/syslog with output that is long",
                "[tool_use: Read] reading file at path /home/ubuntu/memstem/x",
                "[tool_result] file contents that go on for a while as well",
                "[tool_use: Bash] running command number two across many lines",
                "[tool_result] /var/log/syslog with more output as well too",
                "[tool_use: Read] reading another file at /home/ubuntu/y/z",
                "[tool_result] more file contents that go on for a while too",
            ]
        )
        assert is_tool_dump(body) is True

    def test_session_with_prose_not_a_dump(self) -> None:
        # A real conversation with mixed prose and tool calls must pass.
        body = "\n".join(
            [
                "**User:** Can you help me debug the deploy script?",
                "**Assistant:** I'll check the logs first to understand the failure.",
                "[tool_use: Bash] tail -n 50 /var/log/deploy.log",
                "[tool_result] Error: connection refused on port 443",
                "**Assistant:** That's a TLS handshake failure. Let me investigate.",
                "**User:** Thanks, please walk me through what you find.",
                "**Assistant:** Looking at the certificate chain on the server.",
            ]
        )
        assert is_tool_dump(body) is False

    def test_too_few_lines_not_a_dump(self) -> None:
        body = '{"a": 1}\n{"b": 2}\n{"c": 3}'  # only 3 lines, well under min
        assert is_tool_dump(body) is False

    def test_empty_body(self) -> None:
        assert is_tool_dump("") is False


# --- noise_filter end-to-end ---


class TestNoiseFilter:
    def test_normal_memory_kept(self) -> None:
        decision = noise_filter(_record(body="A note about an architecture decision."))
        assert decision.action is NoiseAction.KEEP
        assert decision.kind is None

    def test_heartbeat_dropped(self) -> None:
        decision = noise_filter(_record(body="HEARTBEAT_OK"))
        assert decision.action is NoiseAction.DROP
        assert decision.kind == "heartbeat"
        assert decision.reason

    def test_cron_dropped(self) -> None:
        decision = noise_filter(_record(body="Running __openclaw_test_dream__ at 04:00"))
        assert decision.action is NoiseAction.DROP
        assert decision.kind == "cron_output"

    def test_tool_dump_dropped(self) -> None:
        body = "\n".join(['{"output": "result with long enough text to push past min chars"}'] * 10)
        decision = noise_filter(_record(body=body))
        assert decision.action is NoiseAction.DROP
        assert decision.kind == "tool_dump"

    def test_empty_body_kept(self) -> None:
        # Empty bodies aren't noise-filtered; downstream handles them.
        decision = noise_filter(_record(body=""))
        assert decision.action is NoiseAction.KEEP

    def test_decision_is_immutable(self) -> None:
        # The dataclass is frozen so callers can't mutate the result.
        from dataclasses import FrozenInstanceError

        decision = NoiseDecision(action=NoiseAction.DROP, kind="heartbeat")
        with pytest.raises(FrozenInstanceError):
            decision.action = NoiseAction.KEEP  # type: ignore[misc]


# --- Pipeline integration ---


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


class TestPipelineIntegration:
    def test_keep_record_creates_memory(self, vault: Vault, index: Index) -> None:
        pipeline = Pipeline(vault, index)
        memory = pipeline.process(_record(body="Some legitimate content about a project."))
        assert memory is not None
        assert memory.body.startswith("Some legitimate content")

    def test_dropped_heartbeat_returns_none(
        self, vault: Vault, index: Index, caplog: pytest.LogCaptureFixture
    ) -> None:
        pipeline = Pipeline(vault, index)
        with caplog.at_level("INFO", logger="memstem.core.pipeline"):
            result = pipeline.process(_record(body="HEARTBEAT_OK", ref="/heartbeat-1.md"))
        assert result is None
        # The drop is logged so operators can monitor what's filtered.
        messages = [record.message for record in caplog.records]
        assert any("noise filter dropped" in msg for msg in messages)
        assert any("heartbeat" in msg for msg in messages)

    def test_dropped_cron_returns_none(self, vault: Vault, index: Index) -> None:
        pipeline = Pipeline(vault, index)
        result = pipeline.process(_record(body="__openclaw_dream__ ran at 04:00", ref="/cron-1.md"))
        assert result is None

    def test_dropped_tool_dump_returns_none(self, vault: Vault, index: Index) -> None:
        pipeline = Pipeline(vault, index)
        body = "\n".join(['{"output": "long enough text to exceed the minimum chars"}'] * 10)
        result = pipeline.process(_record(body=body, ref="/dump-1.md"))
        assert result is None

    def test_dropped_record_not_persisted_to_index(self, vault: Vault, index: Index) -> None:
        pipeline = Pipeline(vault, index)
        result = pipeline.process(_record(body="HEARTBEAT_OK", ref="/heartbeat-2.md"))
        assert result is None
        rows = index.db.execute("SELECT id FROM memories").fetchall()
        assert rows == []

    def test_dropped_record_not_in_record_map(self, vault: Vault, index: Index) -> None:
        # The source-ref → id map must NOT get an entry for filtered records,
        # so a later non-noisy record at the same ref still gets a fresh id.
        pipeline = Pipeline(vault, index)
        pipeline.process(_record(body="HEARTBEAT_OK", ref="/x.md"))
        rows = index.db.execute(
            "SELECT memory_id FROM record_map WHERE source = ? AND ref = ?",
            ("openclaw", "/x.md"),
        ).fetchall()
        assert rows == []

    def test_idempotent_drop(self, vault: Vault, index: Index) -> None:
        # Re-emitting the same noisy record is a no-op, not an error.
        pipeline = Pipeline(vault, index)
        first = pipeline.process(_record(body="HEARTBEAT_OK", ref="/x.md"))
        second = pipeline.process(_record(body="HEARTBEAT_OK", ref="/x.md"))
        assert first is None
        assert second is None
