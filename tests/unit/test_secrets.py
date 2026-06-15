"""Secret resolution: env > app-private file > keychain > map, and the file store
(the desktop default — read without an OS keychain prompt)."""

from __future__ import annotations

import pytest

from sportsdata_agents import secrets

pytestmark = pytest.mark.unit


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_file_secret_round_trips_and_is_owner_only(data_dir) -> None:
    assert secrets.get_file_secret("ANTHROPIC_API_KEY") is None
    assert secrets.set_file_secret("ANTHROPIC_API_KEY", "sk-test-123") is True
    assert secrets.get_file_secret("ANTHROPIC_API_KEY") == "sk-test-123"
    mode = (secrets._secrets_file().stat().st_mode & 0o777)
    assert mode == 0o600, f"secrets file must be owner-only, got {oct(mode)}"


def test_resolution_prefers_file_over_keychain(data_dir, monkeypatch) -> None:
    # the keychain would return a stale value, but the file must win — and crucially
    # the keychain is never consulted (no prompt) when the file has the key.
    monkeypatch.delenv("ZZ_TEST_KEY", raising=False)  # ensure the env tier is empty
    called = {"keychain": 0}

    def _kc(name: str):
        called["keychain"] += 1
        return "from-keychain"

    monkeypatch.setattr(secrets, "get_keychain_secret", _kc)
    secrets.set_file_secret("ZZ_TEST_KEY", "from-file")
    assert secrets.resolve_secret("ZZ_TEST_KEY").get_secret_value() == "from-file"
    assert called["keychain"] == 0  # file short-circuited before the keychain


def test_env_still_wins_over_everything(data_dir, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    secrets.set_file_secret("OPENAI_API_KEY", "from-file")
    assert secrets.resolve_secret("OPENAI_API_KEY").get_secret_value() == "from-env"


def test_delete_file_secret(data_dir) -> None:
    secrets.set_file_secret("X", "y")
    assert secrets.delete_file_secret("X") is True
    assert secrets.get_file_secret("X") is None
