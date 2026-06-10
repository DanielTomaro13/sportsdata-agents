"""M0.2 — config, secrets, and workspace tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from sportsdata_agents.config import Settings
from sportsdata_agents.secrets import MissingSecretError, SecretRef, resolve_secret
from sportsdata_agents.workspace import Budgets, Workspace, default_workspace

pytestmark = pytest.mark.unit


# ── Settings ──────────────────────────────────────────────────────────────


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # litellm calls load_dotenv() at import time, so .env values can be sitting in
    # os.environ by the time this runs — clear our prefix to test true defaults.
    import os

    for key in list(os.environ):
        if key.startswith("SPORTSDATA_AGENTS_"):
            monkeypatch.delenv(key)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.env == "dev"
    assert s.default_tenant == "local"
    assert s.mcp_command == ["sportsdata-mcp"]
    assert "postgresql" in s.database_url


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DEFAULT_TENANT", "acme")
    monkeypatch.setenv("SPORTSDATA_AGENTS_LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.default_tenant == "acme"
    assert s.log_level == "DEBUG"


def test_settings_loads_from_env_file(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("SPORTSDATA_AGENTS_DEFAULT_WORKSPACE=fromfile\nSPORTSDATA_AGENTS_ENV=prod\n")
    s = Settings(_env_file=str(env))  # type: ignore[call-arg]
    assert s.default_workspace == "fromfile"
    assert s.env == "prod"


# ── Secret resolution ───────────────────────────────────────────────────────


def test_resolve_secret_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAGOLF_KEY", "from-env")
    out = resolve_secret(SecretRef(name="DATAGOLF_KEY"), extra={"DATAGOLF_KEY": "from-map"})
    assert isinstance(out, SecretStr)
    assert out.get_secret_value() == "from-env"


def test_resolve_secret_falls_back_to_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAGOLF_KEY", raising=False)
    out = resolve_secret("DATAGOLF_KEY", extra={"DATAGOLF_KEY": "from-map"})
    assert out.get_secret_value() == "from-map"


def test_resolve_secret_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(MissingSecretError) as ei:
        resolve_secret("NOPE_KEY")
    assert ei.value.name == "NOPE_KEY"
    assert "NOPE_KEY" in str(ei.value)


def test_secret_does_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "supersecret")
    out = resolve_secret("TOKEN")
    assert "supersecret" not in repr(out)


def test_secret_maps_do_not_leak_in_reprs() -> None:
    """The secrets maps on Settings and Workspace are repr-hidden (§13)."""
    s = Settings(_env_file=None, secrets={"K": "supersecret-settings"})  # type: ignore[call-arg]
    ws = Workspace(secrets={"K": "supersecret-workspace"})
    assert "supersecret-settings" not in repr(s)
    assert "supersecret-workspace" not in repr(ws)


# ── Workspace ────────────────────────────────────────────────────────────────


def test_default_workspace_from_settings() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    ws = default_workspace(s)
    assert ws.tenant_id == "local" and ws.workspace_id == "local"
    assert ws.provisioning == "byo"
    assert isinstance(ws.budgets, Budgets)
    assert ws.budgets.per_run_usd > 0


def test_workspace_resolve_secret_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WS_KEY", raising=False)
    s = Settings(_env_file=None, secrets={"WS_KEY": "from-settings"})  # type: ignore[call-arg]
    ws = Workspace(secrets={"WS_KEY": "from-workspace"})
    # workspace map shadows settings map
    assert ws.resolve_secret("WS_KEY", settings=s).get_secret_value() == "from-workspace"
    # env still wins over both
    monkeypatch.setenv("WS_KEY", "from-env")
    assert ws.resolve_secret("WS_KEY", settings=s).get_secret_value() == "from-env"


def test_mcp_command_tolerates_every_env_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sourcing .env through a shell strips the JSON quotes ([/path]) — observed
    crashing the gateway at startup. All three arrival shapes must parse."""
    import os

    for key in list(os.environ):
        if key.startswith("SPORTSDATA_AGENTS_"):
            monkeypatch.delenv(key)
    cases = {
        '["/abs/sportsdata-mcp", "serve"]': ["/abs/sportsdata-mcp", "serve"],  # proper JSON
        "[/abs/sportsdata-mcp]": ["/abs/sportsdata-mcp"],  # shell-mangled
        "/abs/sportsdata-mcp serve": ["/abs/sportsdata-mcp", "serve"],  # plain command
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("SPORTSDATA_AGENTS_MCP_COMMAND", raw)
        assert Settings(_env_file=None).mcp_command == expected  # type: ignore[call-arg]
