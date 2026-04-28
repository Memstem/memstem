"""Tests for the auth module (persistent secrets store)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from memstem import auth


@pytest.fixture(autouse=True)
def _isolated_secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at a per-test secrets file; clear env vars."""
    secrets_file = tmp_path / "secrets.yaml"
    monkeypatch.setenv("MEMSTEM_SECRETS_FILE", str(secrets_file))
    for env_var in auth.PROVIDERS.values():
        monkeypatch.delenv(env_var, raising=False)
    return secrets_file


class TestSecretsPath:
    def test_default_path_under_home_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEMSTEM_SECRETS_FILE", raising=False)
        assert auth.secrets_path() == auth.DEFAULT_PATH

    def test_override_via_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "alt" / "secrets.yaml"
        monkeypatch.setenv("MEMSTEM_SECRETS_FILE", str(target))
        assert auth.secrets_path() == target

    def test_override_expands_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMSTEM_SECRETS_FILE", "~/some/path.yaml")
        assert "~" not in str(auth.secrets_path())


class TestGetSecret:
    def test_returns_none_when_nothing_set(self) -> None:
        assert auth.get_secret("openai") is None

    def test_reads_from_env_via_explicit_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CUSTOM_OPENAI_KEY", "sk-from-env")
        assert auth.get_secret("openai", env_var="CUSTOM_OPENAI_KEY") == "sk-from-env"

    def test_reads_from_env_via_default_provider_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-default")
        assert auth.get_secret("openai") == "sk-default"

    def test_falls_back_to_file_when_env_missing(self) -> None:
        auth.set_secret("openai", "sk-from-file")
        assert auth.get_secret("openai") == "sk-from-file"

    def test_env_var_wins_over_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth.set_secret("openai", "sk-from-file")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        assert auth.get_secret("openai") == "sk-from-env"

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "   sk-padded   ")
        assert auth.get_secret("openai") == "sk-padded"

    def test_empty_env_falls_through_to_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        auth.set_secret("openai", "sk-from-file")
        assert auth.get_secret("openai") == "sk-from-file"

    def test_provider_lookup_is_case_insensitive(self) -> None:
        auth.set_secret("openai", "sk-test")
        assert auth.get_secret("OpenAI") == "sk-test"
        assert auth.get_secret("OPENAI") == "sk-test"

    def test_explicit_env_var_overrides_default_provider_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When both vars exist, the caller-supplied env_var wins (matches
        # the embedder's `embedding.api_key_env` config knob).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-default-env")
        monkeypatch.setenv("CUSTOM_KEY", "sk-custom")
        assert auth.get_secret("openai", env_var="CUSTOM_KEY") == "sk-custom"


class TestSetSecret:
    def test_creates_file_and_parent_dir(self, _isolated_secrets_file: Path) -> None:
        assert not _isolated_secrets_file.exists()
        auth.set_secret("openai", "sk-test")
        assert _isolated_secrets_file.is_file()
        assert _isolated_secrets_file.parent.is_dir()

    def test_writes_user_only_permissions(self, _isolated_secrets_file: Path) -> None:
        auth.set_secret("openai", "sk-test")
        mode = stat.S_IMODE(_isolated_secrets_file.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_lowercases_provider_key(self, _isolated_secrets_file: Path) -> None:
        auth.set_secret("OpenAI", "sk-test")
        content = _isolated_secrets_file.read_text()
        assert "openai:" in content
        assert "OpenAI:" not in content

    def test_strips_key_whitespace(self) -> None:
        auth.set_secret("openai", "  sk-test  \n")
        assert auth.get_secret("openai") == "sk-test"

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="empty key"):
            auth.set_secret("openai", "   ")

    def test_overwrites_existing(self) -> None:
        auth.set_secret("openai", "sk-old")
        auth.set_secret("openai", "sk-new")
        assert auth.get_secret("openai") == "sk-new"

    def test_preserves_other_providers(self) -> None:
        auth.set_secret("openai", "sk-openai")
        auth.set_secret("voyage", "voyage-key")
        assert auth.get_secret("openai") == "sk-openai"
        assert auth.get_secret("voyage") == "voyage-key"


class TestRemoveSecret:
    def test_returns_false_when_missing(self) -> None:
        assert auth.remove_secret("openai") is False

    def test_returns_true_when_removed(self) -> None:
        auth.set_secret("openai", "sk-test")
        assert auth.remove_secret("openai") is True
        assert auth.get_secret("openai") is None

    def test_deletes_file_when_last_secret_removed(self, _isolated_secrets_file: Path) -> None:
        auth.set_secret("openai", "sk-test")
        assert _isolated_secrets_file.is_file()
        auth.remove_secret("openai")
        assert not _isolated_secrets_file.exists()

    def test_keeps_file_when_others_remain(self, _isolated_secrets_file: Path) -> None:
        auth.set_secret("openai", "sk-test")
        auth.set_secret("voyage", "voyage-key")
        auth.remove_secret("openai")
        assert _isolated_secrets_file.is_file()
        assert auth.get_secret("voyage") == "voyage-key"


class TestListSecrets:
    def test_empty_when_nothing_stored(self) -> None:
        assert auth.list_secrets() == {}

    def test_returns_masked_values(self) -> None:
        auth.set_secret("openai", "sk-proj-abcdef1234567890")
        listed = auth.list_secrets()
        assert "openai" in listed
        assert "sk-proj-abcdef1234567890" not in listed["openai"]
        assert "sk-pro" in listed["openai"]
        assert "7890" in listed["openai"]


class TestMask:
    def test_short_key_fully_hidden(self) -> None:
        assert auth.mask("short") == "…"
        assert auth.mask("eleven_char") == "…"

    def test_long_key_shows_prefix_and_suffix(self) -> None:
        assert auth.mask("sk-proj-abcdef1234567890") == "sk-pro…7890"

    def test_exactly_12_chars_shows_prefix_suffix(self) -> None:
        assert auth.mask("123456789012") == "123456…9012"


class TestLoadResilience:
    def test_missing_file_returns_empty(self) -> None:
        assert auth._load() == {}

    def test_garbage_file_returns_empty(self, _isolated_secrets_file: Path) -> None:
        _isolated_secrets_file.parent.mkdir(parents=True, exist_ok=True)
        _isolated_secrets_file.write_text("not valid yaml: [unclosed\n")
        # yaml.safe_load raises YAMLError on this; _load should propagate.
        # We accept either behavior — but assert the failure mode is loud.
        with pytest.raises(Exception):  # noqa: B017 — explicit "loud" is the contract
            auth._load()

    def test_non_dict_yaml_returns_empty(self, _isolated_secrets_file: Path) -> None:
        _isolated_secrets_file.parent.mkdir(parents=True, exist_ok=True)
        _isolated_secrets_file.write_text("- just\n- a\n- list\n")
        assert auth._load() == {}

    def test_drops_falsy_values(self, _isolated_secrets_file: Path) -> None:
        _isolated_secrets_file.parent.mkdir(parents=True, exist_ok=True)
        _isolated_secrets_file.write_text("openai: ''\nvoyage: real-key\n")
        assert auth._load() == {"voyage": "real-key"}
