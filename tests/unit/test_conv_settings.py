"""Workbench B2 — per-conversation model + provider scope.

Covers the settings routes, the /message threading (tier + deny-set reach
session.run), and the harness seam: schemas filtered, execution refused,
conversation tier beats the per-agent pin, and contextvars propagate.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from sportsdata_agents.agents import model_prefs
from sportsdata_agents.agents.harness import (
    CURRENT_CONV_TIER,
    CURRENT_MCP_DENY,
    Harness,
    ToolDef,
)
from sportsdata_agents.gateway.app import create_app
from sportsdata_agents.models.gateway import ModelReply
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "data"))


class RecordingSession:
    agent_name = "orchestrator"
    recorder = None

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def run(self, prompt: str, **kw: Any) -> Any:
        self.kwargs.append(kw)
        from sportsdata_agents.agents.harness import RunResult

        return RunResult(output="ok", stop_reason="done", steps=1, tool_call_count=0, cost_usd=0.0)


class SettingsStubStore:
    """Endpoint-wiring stub with B2 settings (no DB)."""

    known: ClassVar[set[str]] = {"web-1"}

    def __init__(self) -> None:
        self.settings: dict[str, dict[str, Any]] = {}

    async def list_conversations(self, *, include_archived: bool = False) -> list:
        return []

    async def messages_for(self, key: str):
        return None

    async def context_for(self, key: str):
        return None

    async def append_turn(self, key: str, user_text: str, answer_text: str) -> None:
        return None

    async def set_settings(self, key: str, *, model_tier, mcp_providers) -> bool:
        self.settings[key] = {"model_tier": model_tier, "mcp_providers": mcp_providers}
        return True

    async def settings_for(self, key: str):
        return self.settings.get(key)


class _FakeMCPManager:
    """Warms the gateway's provider-universe cache without a real subprocess."""

    def __init__(self, *a: Any, **k: Any) -> None: ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: Any) -> None: ...

    async def call_tool(self, name: str, args: dict) -> dict:
        return {
            "available": {
                "afl.matches": {"provider": "afl", "tools": 3},
                "nba.stats": {"provider": "nba", "tools": 4},
                "sportsbet.sports": {"provider": "sportsbet", "tools": 5},
            },
            "providers": {},
        }


@pytest.fixture
async def wired(monkeypatch):
    import sportsdata_agents.mcp.manager as mcp_manager

    monkeypatch.setattr(mcp_manager, "MCPManager", _FakeMCPManager)
    session = RecordingSession()
    store = SettingsStubStore()
    app = create_app(session=session, conversation_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c, session, store


# ── routes ───────────────────────────────────────────────────────────────


async def test_settings_roundtrip_and_validation(wired):
    client, _session, store = wired
    # defaults for an unknown/unsaved chat — not an error
    r = await client.get("/conversations/web-new/settings")
    assert r.status_code == 200
    assert r.json() == {"model_tier": None, "mcp_providers": None}

    r = await client.post(
        "/conversations/web-1/settings",
        json={"model_tier": "fast", "mcp_providers": ["afl", "nba"]},
    )
    assert r.status_code == 200
    assert store.settings["web-1"] == {"model_tier": "fast", "mcp_providers": ["afl", "nba"]}

    assert (
        await client.post("/conversations/web-1/settings", json={"model_tier": "shiny"})
    ).status_code == 400
    assert (
        await client.post("/conversations/web-1/settings", json={"mcp_providers": "afl"})
    ).status_code == 400


async def test_message_threads_settings_into_run(wired):
    client, session, store = wired
    store.settings["web-1"] = {"model_tier": "strong", "mcp_providers": ["afl", "nba"]}
    r = await client.post("/message", json={"text": "hi", "conversation_id": "web-1"})
    assert r.status_code == 200
    kw = session.kwargs[-1]
    assert kw["tier"] == "strong"
    # universe {afl, nba, sportsbet} minus allowed {afl, nba} → deny {sportsbet}
    assert kw["mcp_deny"] == frozenset({"sportsbet"})


async def test_message_without_settings_passes_defaults(wired):
    client, session, _store = wired
    r = await client.post("/message", json={"text": "hi", "conversation_id": "web-2"})
    assert r.status_code == 200
    # no settings → the kwargs are simply absent (plain sessions keep working)
    kw = session.kwargs[-1]
    assert "tier" not in kw and "mcp_deny" not in kw


# ── harness seam ─────────────────────────────────────────────────────────


class TierRecordingProvider:
    def __init__(self) -> None:
        self.tiers: list[str] = []
        self.schemas: list[list[str]] = []

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, tools=None, **kw):  # type: ignore[no-untyped-def]
        self.tiers.append(tier)
        self.schemas.append([t["function"]["name"] for t in (tools or [])])
        reply = ModelReply(text="done", model="fake", tokens_in=10, tokens_out=5, cost_usd=0.01)
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


def _tool(name: str) -> ToolDef:
    async def execute(args: dict[str, Any]) -> Any:
        return {"ok": name}

    return ToolDef(name=name, description=name, parameters={"type": "object"}, execute=execute)


def _spec():
    from sportsdata_agents.agents.spec import AgentSpec

    return AgentSpec.model_validate(
        {"id": "test_agent", "display_name": "T", "system_prompt": "x", "model_tier": "balanced"}
    )


async def test_deny_hides_and_refuses_scoped_tools():
    provider = TierRecordingProvider()
    h = Harness(
        _spec(),
        provider=provider,
        workspace=WS,
        tools=[_tool("sportsbet_event_markets"), _tool("afl_matches_list"), _tool("find_arbs")],
    )
    token = CURRENT_MCP_DENY.set(frozenset({"sportsbet"}))
    try:
        await h.run("q")
        # the scoped-out provider's tool is not offered; natives untouched
        assert provider.schemas[-1] == ["afl_matches_list", "find_arbs"]
        # …and refused outright even if called by name
        msg, ok = await h._execute_tool_inner("sportsbet_event_markets", {})
        assert not ok and "scope" in msg
        _msg2, ok2 = await h._execute_tool_inner("afl_matches_list", {})
        assert ok2
    finally:
        CURRENT_MCP_DENY.reset(token)
    # scope gone → tool offered again
    await h.run("q")
    assert "sportsbet_event_markets" in provider.schemas[-1]


async def test_conv_tier_beats_agent_pin():
    provider = TierRecordingProvider()
    h = Harness(_spec(), provider=provider, workspace=WS)
    model_prefs.set_override("test_agent", "fast")  # B3 pin
    token = CURRENT_CONV_TIER.set("strong")  # B2 conversation force
    try:
        await h.run("q", tier="balanced")
    finally:
        CURRENT_CONV_TIER.reset(token)
    assert provider.tiers[-1] == "strong"
    # without the conversation force, the pin rules again
    await h.run("q", tier="balanced")
    assert provider.tiers[-1] == "fast"


async def test_team_session_sets_and_resets_contextvars():
    from sportsdata_agents.gateway.service import TeamSession

    seen: dict[str, Any] = {}

    class FakeRuntime:
        async def run(self, prompt: str, *, recorder=None):
            seen["tier"] = CURRENT_CONV_TIER.get()
            seen["deny"] = CURRENT_MCP_DENY.get()
            from sportsdata_agents.agents.harness import RunResult

            return RunResult(output="ok", stop_reason="done", steps=1, tool_call_count=0, cost_usd=0.0)

    session = TeamSession.__new__(TeamSession)  # skip __init__ — only run() is under test
    session._runtime = FakeRuntime()
    await session.run("q", tier="fast", mcp_deny=frozenset({"tab"}))
    assert seen == {"tier": "fast", "deny": frozenset({"tab"})}
    # reset after the run — nothing leaks into the next request's context
    assert CURRENT_CONV_TIER.get() is None and CURRENT_MCP_DENY.get() is None
