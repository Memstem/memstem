"""Sync HTTP client for talking to a local Memstem daemon.

The daemon co-hosts an HTTP API on loopback (`POST /search`,
`GET /memory/{id}`, `GET /health`) so first-party clients can call
into the same `Search` / `Vault` / `Index` instances the watch loop
uses, without spawning a per-query subprocess. ADR 0014 makes the
CLI a thin client over that API when a daemon is reachable, falling
back to direct-DB only when one isn't.

Why the wrapper exists
----------------------
The HTTP server is fully async (FastAPI/uvicorn). The CLI, however,
is sync `typer` code, and we don't want to drag an event loop into
every `memstem search` invocation just to make one HTTP call. A small
sync `httpx.Client` wrapper is the right shape: tight timeouts, no
event loop, no spurious cold-start cost. The wrapper is deliberately
narrow — it does not try to replace `Search` for callers that need
ranking metadata; it just covers the surface the CLI uses today.

Discovery and fallback
----------------------
:func:`find_daemon` probes the configured loopback URL with a tight
timeout. If the daemon answers `/health` and reports the same vault
path the CLI was configured for, the caller gets a `DaemonClient` and
should route through it. If the probe fails — connection refused,
timeout, vault mismatch, or any HTTP-level error — the function
returns ``None`` and the caller falls back to direct-DB. The fallback
must be transparent; users without a running daemon should see no
behavior change, only slightly higher latency.

The vault-path equality check is non-negotiable. A daemon serving a
*different* vault than the one the CLI is configured against must not
silently answer queries — the user would get results from the wrong
index without ever seeing the mismatch. We compare resolved absolute
paths so symlinks and relative paths don't produce false negatives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from memstem.config import Config

logger = logging.getLogger(__name__)


DEFAULT_HEALTH_TIMEOUT = 0.25
"""Probe timeout when discovering whether a daemon is reachable. Tight
on purpose: a misconfigured or hung daemon must not make the CLI feel
slower than the direct-DB fallback. 250 ms is comfortably above
loopback HTTP latency and below the threshold where users notice."""

DEFAULT_REQUEST_TIMEOUT = 30.0
"""Per-request timeout for actual search/get calls. Generous enough to
cover an embedder round-trip on the daemon side (text-embedding-3-large
typically responds in <1 s but tolerates network jitter) without
hanging the CLI indefinitely if the daemon is wedged."""


@dataclass(frozen=True, slots=True)
class DaemonHealth:
    """Subset of the daemon's `/health` payload we depend on."""

    version: str
    vault: Path
    embedder: bool


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result row, mirroring the daemon's `/search` SearchHit shape.

    Kept as a plain dataclass (not a Pydantic model) so the CLI
    rendering path doesn't need to import FastAPI's Pydantic types.
    The fields match the HTTP server's serializer one-to-one.
    """

    id: str
    title: str | None
    type: str
    snippet: str
    score: float
    path: str
    bm25_rank: int | None
    vec_rank: int | None
    frontmatter: dict[str, Any]


class DaemonError(Exception):
    """Raised when a daemon HTTP call fails in a way the caller should
    surface. Connection-refused / probe-failure cases are handled by
    :func:`find_daemon` returning ``None`` instead of raising — those
    are not errors, they're "no daemon available, fall back to direct
    mode"."""


class DaemonClient:
    """Thin sync HTTP client over a Memstem daemon's loopback API.

    The client owns a single `httpx.Client` for connection reuse
    within a CLI invocation. CLI invocations are short-lived so the
    one-connection model is sufficient; long-running embedders use
    `httpx.AsyncClient` directly via the daemon, not this class.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DaemonClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def health(self, *, timeout: float = DEFAULT_HEALTH_TIMEOUT) -> DaemonHealth | None:
        """Probe ``/health``; return parsed payload on 200 OK, ``None``
        on any failure. Failures are not raised because health is
        discovery, not a request — a missing daemon is the expected
        case for many users.
        """
        try:
            resp = self._client.get("/health", timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("daemon health probe failed: %s", exc)
            return None

        try:
            data = resp.json()
            return DaemonHealth(
                version=str(data["version"]),
                vault=Path(data["vault"]),
                embedder=bool(data["embedder"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.debug("daemon health payload malformed: %s — body=%r", exc, resp.text)
            return None

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        types: list[str] | None = None,
        rrf_k: int | None = None,
        bm25_weight: float | None = None,
        vector_weight: float | None = None,
        importance_weight: float | None = None,
        type_bias: dict[str, float] | None = None,
    ) -> list[SearchHit]:
        """Hybrid search via ``POST /search``. Raises :class:`DaemonError`
        on any HTTP-level failure so the caller can decide whether to
        fall back or surface the error."""
        body: dict[str, Any] = {"query": query, "limit": limit}
        if types is not None:
            body["types"] = types
        if rrf_k is not None:
            body["rrf_k"] = rrf_k
        if bm25_weight is not None:
            body["bm25_weight"] = bm25_weight
        if vector_weight is not None:
            body["vector_weight"] = vector_weight
        if importance_weight is not None:
            body["importance_weight"] = importance_weight
        if type_bias is not None:
            body["type_bias"] = type_bias

        try:
            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise DaemonError(f"daemon /search failed: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise DaemonError(f"daemon /search returned non-JSON: {resp.text!r}") from exc
        if not isinstance(payload, list):
            raise DaemonError(f"daemon /search returned non-list payload: {payload!r}")

        return [_hit_from_dict(item) for item in payload]


def _hit_from_dict(item: dict[str, Any]) -> SearchHit:
    """Lift a daemon SearchHit JSON object into our dataclass.

    Defensive defaults for the optional fields: `title`, `bm25_rank`,
    `vec_rank` are nullable on the wire. Unknown extra fields are
    ignored so adding a field on the daemon side doesn't break older
    clients.
    """
    return SearchHit(
        id=str(item["id"]),
        title=item.get("title"),
        type=str(item["type"]),
        snippet=str(item.get("snippet", "")),
        score=float(item["score"]),
        path=str(item["path"]),
        bm25_rank=item.get("bm25_rank"),
        vec_rank=item.get("vec_rank"),
        frontmatter=dict(item.get("frontmatter") or {}),
    )


def find_daemon(
    config: Config,
    *,
    timeout: float = DEFAULT_HEALTH_TIMEOUT,
) -> DaemonClient | None:
    """Return a connected `DaemonClient` if a usable daemon is
    serving the same vault as the calling CLI; ``None`` otherwise.

    "Usable" means three things, in order:

    1. The HTTP server config says HTTP is enabled.
    2. ``GET /health`` answers within ``timeout``.
    3. The daemon's reported `vault` path resolves to the same
       absolute path the CLI is configured against.

    Any failure returns ``None`` and the caller is expected to fall
    back to direct-DB. The vault-mismatch case is logged at info-
    level so users running multiple vaults can see why their CLI
    isn't using the daemon they expect.
    """
    http = config.http
    if not http.enabled:
        return None

    base_url = f"http://{http.host}:{http.port}"
    client = DaemonClient(base_url)
    health = client.health(timeout=timeout)
    if health is None:
        client.close()
        return None

    expected_vault = config.vault_path.expanduser().resolve()
    daemon_vault = health.vault.expanduser().resolve()
    if expected_vault != daemon_vault:
        logger.info(
            "daemon at %s is serving vault %s but CLI is configured for %s; "
            "falling back to direct DB access",
            base_url,
            daemon_vault,
            expected_vault,
        )
        client.close()
        return None

    return client


__all__ = [
    "DEFAULT_HEALTH_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "DaemonClient",
    "DaemonError",
    "DaemonHealth",
    "SearchHit",
    "find_daemon",
]
