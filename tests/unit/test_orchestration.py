"""M0.8 — native tools, the MCP→ToolDef bridge, and delegation (all offline)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from sportsdata_agents.agents.runtime import AgentRuntime, delegate_tool
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.mcp.toolset import CapabilityResolutionError, bridge_mcp_tools
from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
from sportsdata_agents.tools.registry import NATIVE_TOOLS, get_native_tools
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


# ── native tools: golden values ──────────────────────────────────────────


async def test_implied_probability_golden() -> None:
    out = await NATIVE_TOOLS["implied_probability"].execute({"odds": 2.50})
    assert out["probability"] == pytest.approx(0.4)


async def test_implied_probability_rejects_subunit_odds() -> None:
    with pytest.raises(ValueError, match=r">= 1\.01"):
        await NATIVE_TOOLS["implied_probability"].execute({"odds": 0.9})


async def test_vig_removal_golden() -> None:
    # 1.90/1.90 two-way market: implied 0.5263 each, overround ~1.0526, fair 0.5/0.5
    out = await NATIVE_TOOLS["vig_removal"].execute(
        {"prices": [{"name": "home", "odds": 1.90}, {"name": "away", "odds": 1.90}]}
    )
    assert out["overround"] == pytest.approx(1.0526, abs=1e-3)
    assert out["vig_pct"] == pytest.approx(5.26, abs=0.01)
    for fp in out["fair_probabilities"]:
        assert fp["probability"] == pytest.approx(0.5)


async def test_best_price_golden() -> None:
    out = await NATIVE_TOOLS["best_price"].execute(
        {"prices": [{"book": "tab", "odds": 1.95}, {"book": "pinnacle", "odds": 2.02}, {"book": "betr", "odds": 1.98}]}
    )
    assert out == {"book": "pinnacle", "odds": 2.02}


def test_get_native_tools_unknown_fails_loudly() -> None:
    with pytest.raises(KeyError, match="ghost_tool"):
        get_native_tools(["implied_probability", "ghost_tool"])


# ── the MCP→ToolDef bridge ───────────────────────────────────────────────


@dataclass
class _FakeTool:
    name: str
    description: str = "desc"
    inputSchema: dict[str, Any] | None = None  # mirrors the MCP SDK field name


@dataclass
class _FakeManager:
    tools: list[_FakeTool]
    caps: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def list_tools(self) -> list[_FakeTool]:
        return self.tools

    async def tools_for_capability(self, capability: str) -> list[str]:
        return self.caps.get(capability, [])

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, args or {}))
        return {"ok": name}


async def test_bridge_unfiltered_exposes_catalogue() -> None:
    mgr = _FakeManager(tools=[_FakeTool("mlb_teams"), _FakeTool("openf1_sessions")])
    defs = await bridge_mcp_tools(mgr, None)  # type: ignore[arg-type]
    assert {d.name for d in defs} == {"mlb_teams", "openf1_sessions"}


async def test_bridge_filters_by_capability() -> None:
    mgr = _FakeManager(
        tools=[_FakeTool("mlb_teams"), _FakeTool("mlb_boxscore"), _FakeTool("tab_sports")],
        caps={"ref.teams": ["mlb_teams"]},
    )
    defs = await bridge_mcp_tools(mgr, ["ref.teams"])  # type: ignore[arg-type]
    assert {d.name for d in defs} == {"mlb_teams"}


async def test_bridge_zero_tool_capability_fails_loudly() -> None:
    mgr = _FakeManager(tools=[_FakeTool("mlb_teams")], caps={"ref.teams": ["mlb_teams"]})
    with pytest.raises(CapabilityResolutionError, match=r"sport\.ghost"):
        await bridge_mcp_tools(mgr, ["ref.teams", "sport.ghost"])  # type: ignore[arg-type]


async def test_bridge_execute_routes_to_manager() -> None:
    mgr = _FakeManager(tools=[_FakeTool("mlb_teams", inputSchema={"type": "object"})])
    defs = await bridge_mcp_tools(mgr, None)  # type: ignore[arg-type]
    out = await defs[0].execute({"sportId": 1})
    assert out["data"] == {"ok": "mlb_teams"}  # provenance envelope (§13.1)
    assert out["_source"]["tool"] == "mlb_teams"
    assert "fetched_at" in out["_source"]
    assert mgr.calls == [("mlb_teams", {"sportId": 1})]


# ── delegation: specialists-as-tools, isolated contexts ──────────────────


class ScriptedProvider:
    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        self.seen.append([dict(m) for m in messages])
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, model="fake", tokens_in=50, tokens_out=10, cost_usd=0.001)


def _delegate_call(agent_id: str, task: str) -> ModelReply:
    return ModelReply(
        text="",
        model="fake",
        tokens_in=50,
        tokens_out=10,
        cost_usd=0.001,
        tool_calls=(ToolCallRequest(id="d1", name=agent_id, arguments={"task": task}),),
    )


def _spec(id_: str, **overrides: Any) -> AgentSpec:
    base: dict[str, Any] = {"id": id_, "display_name": id_, "system_prompt": f"You are {id_}."}
    base.update(overrides)
    return AgentSpec.model_validate(base)


async def test_delegation_runs_subagent_and_condenses() -> None:
    specialist_provider = ScriptedProvider(_text("42 home runs (source: mlb_stats)"))
    orch_provider = ScriptedProvider(
        _delegate_call("stats_agent", "how many home runs?"),
        _text("The specialist reports 42 home runs."),
    )

    async with (
        AgentRuntime(_spec("stats_agent"), provider=specialist_provider, workspace=WS) as sub,
        AgentRuntime(
            _spec("orch", can_delegate_to=["stats_agent"]),
            provider=orch_provider,
            workspace=WS,
            delegates=[sub],
        ) as orch,
    ):
        res = await orch.run("how many home runs did X hit?")

    assert res.stop_reason == "done"
    assert "42 home runs" in res.output

    # the delegation result arrived condensed (JSON summary) in a tool message
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    summary = json.loads(tool_msgs[0]["content"])
    assert summary["agent"] == "stats_agent"
    assert summary["stop_reason"] == "done"
    assert "42 home runs" in summary["answer"]

    # §8.2 sub-agent isolation: the specialist saw ONLY its task, not the orchestrator's transcript
    specialist_first_messages = specialist_provider.seen[0]
    assert specialist_first_messages[1]["content"] == "how many home runs?"
    assert all("orchestrator" not in (m.get("content") or "") for m in specialist_first_messages)


async def test_delegate_tool_requires_task() -> None:
    async with AgentRuntime(_spec("stats_agent"), provider=ScriptedProvider(_text("x")), workspace=WS) as sub:
        tool = delegate_tool(sub)
        out = await tool.execute({})
        assert "non-empty `task`" in out


async def test_runtime_unknown_native_tool_fails_loudly() -> None:
    spec = _spec("bad", tools={"native": ["ghost_tool"]})
    with pytest.raises(KeyError, match="ghost_tool"):
        async with AgentRuntime(spec, provider=ScriptedProvider(_text("x")), workspace=WS):
            pass


async def test_runtime_rejects_undeclared_delegates() -> None:
    """The spec's can_delegate_to is the ACL — binding an undeclared delegate is refused."""
    async with AgentRuntime(_spec("stats_agent"), provider=ScriptedProvider(_text("x")), workspace=WS) as sub:
        with pytest.raises(ValueError, match="not declared in can_delegate_to"):
            AgentRuntime(_spec("orch"), provider=ScriptedProvider(_text("x")), workspace=WS, delegates=[sub])


async def test_open_team_rejects_nested_delegation() -> None:
    """A specialist with its own delegates would be silently unbound — loud at build time."""
    from sportsdata_agents.agents.runtime import open_team

    specs = {
        "orch": _spec("orch", can_delegate_to=["mid"]),
        "mid": _spec("mid", can_delegate_to=["leaf"]),
        "leaf": _spec("leaf"),
    }
    with pytest.raises(ValueError, match="one level only"):
        async with open_team(specs, "orch", provider=ScriptedProvider(_text("x")), workspace=WS):
            pass


async def test_runtime_cleans_up_manager_when_bridge_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """CapabilityResolutionError AFTER the subprocess spawned must not leak it (M0.4 lesson)."""
    from sportsdata_agents.agents import runtime as rt

    class FakeManager:
        instances: ClassVar[list[FakeManager]] = []

        def __init__(self, **kw: Any) -> None:
            self.entered = False
            self.exited = False
            FakeManager.instances.append(self)

        async def __aenter__(self) -> FakeManager:
            self.entered = True
            return self

        async def __aexit__(self, *a: Any) -> None:
            self.exited = True

        async def list_tools(self) -> list[Any]:
            return []

        async def tools_for_capability(self, capability: str) -> list[str]:
            return []  # → CapabilityResolutionError in the bridge

    monkeypatch.setattr(rt, "MCPManager", FakeManager)
    spec = _spec("capped", tools={"mcp_capabilities": ["sport.prices"]})
    with pytest.raises(Exception, match=r"sport\.prices"):
        async with AgentRuntime(spec, provider=ScriptedProvider(_text("x")), workspace=WS):
            pass
    mgr = FakeManager.instances[-1]
    assert mgr.entered and mgr.exited, "spawned manager leaked after bridge failure"
