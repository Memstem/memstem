#!/usr/bin/env python3
"""MemStem update-check server (ADR 0050) — what runs behind updates.memstem.dev.

Published in the repo on purpose: this file is the complete record of what the
service receives and keeps.

GET /v1/check?v=<version>&os=<linux|darwin|windows>&py=<3.12>&src=<pypi|source>&id=<uuid>
    Returns {"latest": "...", "current": "...", "update_available": bool,
             "changelog": "...", "released": "YYYY-MM-DD"}.
    Records one row per install per UTC day: the day, a keyed hash of `id`
    (the raw id is never stored), version, os, python minor, install source,
    and the two-letter country Cloudflare adds as a header. No IP address,
    hostname, path, username, or vault content is received or stored.
    Every parameter is optional; a request with none of them still gets the
    latest version and records nothing.

GET /v1/stats   (Authorization: Bearer <token from the stats-token file>)
    Aggregates: daily active installs (last 30 days), versions, os, country.

GET /healthz    -> "ok"

Stdlib only. Env: MEMSTEM_UPDATES_DB, MEMSTEM_UPDATES_SECRET_FILE,
MEMSTEM_UPDATES_STATS_TOKEN_FILE, MEMSTEM_UPDATES_PORT (default 8790).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STATE = Path(os.environ.get("MEMSTEM_UPDATES_STATE", "~/.local/share/memstem-updates")).expanduser()
DB_PATH = Path(os.environ.get("MEMSTEM_UPDATES_DB", STATE / "checkins.sqlite3"))
SECRET_FILE = Path(os.environ.get("MEMSTEM_UPDATES_SECRET_FILE", STATE / "id-hash-secret"))
TOKEN_FILE = Path(os.environ.get("MEMSTEM_UPDATES_STATS_TOKEN_FILE", STATE / "stats-token"))
PORT = int(os.environ.get("MEMSTEM_UPDATES_PORT", "8790"))
PYPI_URL = "https://pypi.org/pypi/memstem/json"
CHANGELOG = "https://github.com/Memstem/memstem/blob/main/CHANGELOG.md"
CACHE_SECONDS = 3600

_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([a-z0-9.+-]{0,20})$", re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OS = {"linux", "darwin", "windows", "other"}
_SRC = {"pypi", "source"}
_PY_RE = re.compile(r"^3\.[0-9]{1,2}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

_latest: dict[str, Any] = {"version": None, "released": None, "fetched": 0.0}
_lock = threading.Lock()


def _secret_bytes(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS checkins (day TEXT NOT NULL, install TEXT NOT NULL, "
        "version TEXT, os TEXT, py TEXT, src TEXT, country TEXT, PRIMARY KEY (day, install))"
    )
    return conn


def latest_release() -> tuple[str | None, str | None]:
    with _lock:
        if _latest["version"] and time.time() - float(_latest["fetched"]) < CACHE_SECONDS:
            return _latest["version"], _latest["released"]
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=8) as resp:
            data = json.load(resp)
        version = data["info"]["version"]
        files = data.get("releases", {}).get(version) or []
        released = files[0]["upload_time_iso_8601"][:10] if files else None
    except Exception:
        with _lock:
            return _latest["version"], _latest["released"]
    with _lock:
        _latest.update(version=version, released=released, fetched=time.time())
    return version, released


def _newer(a: str | None, b: str | None) -> bool:
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"[0-9]+", v.split("+")[0])[:4])

    try:
        return bool(a and b and key(a) > key(b))
    except ValueError:
        return False


def record(params: dict[str, str], country: str | None, secret: bytes) -> None:
    raw_id = params.get("id", "").lower()
    if not _UUID_RE.match(raw_id):
        return  # no (valid) id -> nothing recorded
    install = hmac.new(secret, raw_id.encode(), hashlib.sha256).hexdigest()[:32]
    version = params.get("v", "")[:32]
    row = (
        datetime.now(tz=UTC).date().isoformat(),
        install,
        version if _VERSION_RE.match(version) else None,
        params.get("os") if params.get("os") in _OS else None,
        params.get("py") if _PY_RE.match(params.get("py", "")) else None,
        params.get("src") if params.get("src") in _SRC else None,
        country if country and _COUNTRY_RE.match(country) and country != "XX" else None,
    )
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO checkins VALUES (?, ?, ?, ?, ?, ?, ?)", row)


def stats() -> dict[str, object]:
    since = (datetime.now(tz=UTC).date() - timedelta(days=30)).isoformat()
    with _db() as conn:

        def rows(sql: str) -> list[list[object]]:
            return [list(r) for r in conn.execute(sql, (since,))]

        return {
            "daily_active_installs": rows(
                "SELECT day, COUNT(*) FROM checkins WHERE day >= ? GROUP BY day ORDER BY day"
            ),
            "unique_installs_30d": conn.execute(
                "SELECT COUNT(DISTINCT install) FROM checkins WHERE day >= ?", (since,)
            ).fetchone()[0],
            "versions_30d": rows(
                "SELECT version, COUNT(DISTINCT install) n FROM checkins WHERE day >= ? "
                "GROUP BY version ORDER BY n DESC"
            ),
            "os_30d": rows(
                "SELECT os, COUNT(DISTINCT install) n FROM checkins WHERE day >= ? GROUP BY os ORDER BY n DESC"
            ),
            "source_30d": rows(
                "SELECT src, COUNT(DISTINCT install) n FROM checkins WHERE day >= ? GROUP BY src ORDER BY n DESC"
            ),
            "country_30d": rows(
                "SELECT country, COUNT(DISTINCT install) n FROM checkins WHERE day >= ? "
                "GROUP BY country ORDER BY n DESC"
            ),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "memstem-updates/1"
    secret = b""
    token = ""

    def log_message(self, *_args: object) -> None:  # no access log: it would hold IPs
        return

    def _send(self, code: int, body: object) -> None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header(
            "Content-Type", "application/json" if not isinstance(body, bytes) else "text/plain"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._send(200, b"ok")
        if url.path == "/v1/check":
            params = {k: v[0] for k, v in parse_qs(url.query, max_num_fields=8).items() if v}
            try:
                record(params, self.headers.get("CF-IPCountry"), self.secret)
            except Exception:
                pass  # counting must never break the version answer
            latest, released = latest_release()
            current = params.get("v") if _VERSION_RE.match(params.get("v", "")) else None
            return self._send(
                200,
                {
                    "latest": latest,
                    "current": current,
                    "update_available": _newer(latest, current),
                    "changelog": CHANGELOG,
                    "released": released,
                },
            )
        if url.path == "/v1/stats":
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not self.token or not hmac.compare_digest(supplied, self.token):
                return self._send(403, {"error": "forbidden"})
            return self._send(200, stats())
        return self._send(404, {"error": "not found"})


def main() -> None:
    Handler.secret = _secret_bytes(SECRET_FILE)
    Handler.token = _secret_bytes(TOKEN_FILE).decode()
    _db().close()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
