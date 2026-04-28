"""Persistent secrets store for embedder API keys.

Memstem reads API keys from environment variables by default. When no
env var is set, this module falls back to ``~/.config/memstem/secrets.yaml``
— a per-user file outside any vault — so the same key is available
everywhere ``memstem`` runs (cron, PM2, systemd, fresh shells, headless
servers) without having to export the env var in each context.

The file lives outside any project tree, so gitignore is irrelevant.
File permissions are ``0o600`` on write. Override the location with
``MEMSTEM_SECRETS_FILE`` (used by the test suite).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import yaml

DEFAULT_PATH: Final = Path.home() / ".config" / "memstem" / "secrets.yaml"

# Maps provider name (the value of ``embedding.provider`` in config.yaml,
# also the key under which the secret is stored) to its default env var.
# ``ollama`` is local-only and needs no key, so it's not listed.
PROVIDERS: Final[dict[str, str]] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "voyage": "VOYAGE_API_KEY",
}


def secrets_path() -> Path:
    """Resolve the secrets file path, honoring ``MEMSTEM_SECRETS_FILE``."""
    override = os.environ.get("MEMSTEM_SECRETS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_PATH


def _load() -> dict[str, str]:
    p = secrets_path()
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items() if v}


def _save(secrets: dict[str, str]) -> None:
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(dict(secrets), sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def get_secret(provider: str, env_var: str | None = None) -> str | None:
    """Return the API key for ``provider``; env first, then file.

    Checks ``env_var`` if given, otherwise ``PROVIDERS[provider]``. If
    neither is set, falls back to the secrets file. Returns ``None``
    when nothing has a non-empty value. Whitespace is stripped.
    """
    provider = provider.lower()

    env_name = env_var if env_var else PROVIDERS.get(provider)
    if env_name:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val

    return _load().get(provider) or None


def set_secret(provider: str, key: str) -> None:
    """Persist ``key`` to the secrets file under ``provider`` (lowercased)."""
    provider = provider.lower()
    if not key.strip():
        raise ValueError("empty key")
    secrets = _load()
    secrets[provider] = key.strip()
    _save(secrets)


def remove_secret(provider: str) -> bool:
    """Drop ``provider``'s entry. Returns True if something was removed."""
    provider = provider.lower()
    secrets = _load()
    if provider not in secrets:
        return False
    secrets.pop(provider)
    if secrets:
        _save(secrets)
    else:
        secrets_path().unlink(missing_ok=True)
    return True


def list_secrets() -> dict[str, str]:
    """Return all stored secrets with their values masked."""
    return {k: mask(v) for k, v in _load().items()}


def mask(key: str) -> str:
    """Mask a key for display: first 6 + last 4 chars; short keys → ``…``."""
    if len(key) < 12:
        return "…"
    return f"{key[:6]}…{key[-4:]}"


__all__ = [
    "DEFAULT_PATH",
    "PROVIDERS",
    "get_secret",
    "list_secrets",
    "mask",
    "remove_secret",
    "secrets_path",
    "set_secret",
]
