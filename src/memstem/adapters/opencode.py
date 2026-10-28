"""Read-only OpenCode SQLite sessions (including Pocket's OpenRouter/ACP turns)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.adapters.codex import _slugify_cwd

logger = logging.getLogger(__name__)


def _object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("OpenCode data is not an object")
    return value


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def read_sessions(
    path: Path, *, excluded_directories: list[Path], max_session_bytes: int
) -> list[MemoryRecord]:
    """Read one consistent WAL-aware snapshot; never create or change the source."""
    records: list[MemoryRecord] = []
    if not path.is_file():
        return records
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        sessions = db.execute(
            "SELECT id, directory, title, time_created, time_updated FROM session "
            "WHERE parent_id IS NULL ORDER BY time_created, id"
        )
        for sid, cwd, title, created, updated in sessions:
            if not isinstance(sid, str) or not sid or not isinstance(cwd, str) or not cwd:
                logger.warning("Malformed OpenCode session header; retaining prior capture")
                continue
            directory = Path(cwd).expanduser().resolve()
            if any(
                directory.is_relative_to(p.expanduser().resolve()) for p in excluded_directories
            ):
                continue
            size = db.execute(
                "SELECT (SELECT coalesce(sum(length(CAST(data AS BLOB))),0) FROM message "
                "WHERE session_id=?) + (SELECT coalesce(sum(length(CAST(data AS BLOB))),0) "
                "FROM part WHERE session_id=?)",
                (sid, sid),
            ).fetchone()[0]
            if size > max_session_bytes:
                logger.warning(
                    "OpenCode session %s exceeds byte limit; retaining prior capture", sid
                )
                continue
            try:
                turns: list[str] = []
                models: set[tuple[str, str]] = set()
                attribution: list[dict[str, str]] = []
                first_user = ""
                for mid, raw in db.execute(
                    "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created, id",
                    (sid,),
                ):
                    message = _object(raw)
                    role = message.get("role")
                    if role not in ("user", "assistant") or (
                        role == "assistant" and message.get("summary") is True
                    ):
                        continue
                    if role == "assistant" and not message.get("time", {}).get("completed"):
                        continue
                    parts = []
                    for (raw_part,) in db.execute(
                        "SELECT data FROM part WHERE session_id=? AND message_id=? "
                        "ORDER BY time_created, id",
                        (sid, mid),
                    ):
                        part = _object(raw_part)
                        if (
                            part.get("type") != "text"
                            or part.get("synthetic")
                            or part.get("ignored")
                        ):
                            continue
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                    provider, model = message.get("providerID"), message.get("modelID")
                    label = f"**{role.title()}:**"
                    if role == "assistant":
                        if (
                            isinstance(provider, str)
                            and provider
                            and isinstance(model, str)
                            and model
                        ):
                            models.add((provider, model))
                            attribution.append(
                                {"message_id": mid, "provider": provider, "model": model}
                            )
                            label += f" [{provider}/{model}]"
                        else:
                            label += " [model unknown]"
                    if parts:
                        text = "\n".join(parts)
                        turns.append(f"{label} {text}")
                        if role == "user" and not first_user:
                            first_user = text
                    elif role == "assistant":
                        # Tool-only replies still carry actual model attribution, without tool output.
                        turns.append(f"{label} [no visible text]")
                if not first_user:
                    continue
                ref = f"{path.resolve()}#session={sid}"
                tags = ["agent:opencode", _slugify_cwd(cwd)]
                tags += sorted({f"provider:{p}" for p, _ in models})
                tags += sorted({f"model:{p}/{m}" for p, m in models})
                records.append(
                    MemoryRecord(
                        source="opencode",
                        ref=ref,
                        title=first_user.splitlines()[0][:160],
                        body="\n\n".join(turns),
                        tags=[t for t in tags if t],
                        metadata={
                            "type": "session",
                            "session_id": "opencode-" + str(uuid5(NAMESPACE_URL, ref)),
                            "created": _iso(created),
                            "updated": _iso(updated),
                            "opencode": {
                                "session_id": sid,
                                "directory": cwd,
                                "title": title,
                                "messages": attribution,
                            },
                        },
                    )
                )
            except (ValueError, TypeError, AttributeError, OverflowError):
                logger.warning("Malformed OpenCode session %s; retaining prior capture", sid)
    finally:
        db.close()
    return records


class OpenCodeAdapter(Adapter):
    name = "opencode"

    def __init__(
        self,
        *,
        database: Path,
        poll_seconds: float = 30,
        excluded_directories: list[Path] | None = None,
        max_session_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.database = database.expanduser().resolve()
        self.poll_seconds = poll_seconds
        self.excluded_directories = excluded_directories or []
        self.max_session_bytes = max_session_bytes
        self._watching = False
        self._healthy = True

    def source_roots(self) -> list[Path]:
        return [self.database.parent]

    def source_exists(self, ref: str) -> bool:
        return Path(ref.split("#session=", 1)[0]).is_file()

    def watcher_alive(self) -> bool | None:
        return self._healthy if self._watching else None

    async def reconcile(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        try:
            records = await asyncio.to_thread(
                read_sessions,
                self.database,
                excluded_directories=self.excluded_directories,
                max_session_bytes=self.max_session_bytes,
            )
            self._healthy = True
        except (OSError, sqlite3.Error):
            self._healthy = False
            logger.exception("Cannot read OpenCode database %s; will retry", self.database)
            return
        for record in records:
            yield record

    async def watch(self, paths: list[Path]) -> AsyncGenerator[MemoryRecord, None]:
        self._watching = True
        try:
            while True:
                async for record in self.reconcile(paths):
                    yield record
                await asyncio.sleep(self.poll_seconds)
        finally:
            self._watching = False
