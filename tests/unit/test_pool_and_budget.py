"""Shared MCP sessions per scope + one shared budget per team run (§16.1)."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from sportsdata_agents.agents.runtime import AgentRuntime
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.mcp import pool as pool_mod
from sportsdata_agents.mcp.pool import MCPSessionPool
from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
from sportsdata_agents.workspace import Budgets, Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


class FakeManager:
    instances: ClassVar[list[FakeManager]] = []

    def __init__(self, *, groups: list[str] | None = None, command: Any = None, extra_env: Any = None) -> None:
        self.groups = groups or []
        self.entered = False
        self.exited = False
        FakeManager.instances.append(self)

    async def __aenter__(self) -> FakeManager:
        self.entered = True
        return self

    async def __aexit__(self, *a: Any) -> None:
        self.exited = True

    async def list_tools(self) -> list[Any]:
        class T:
            name = "mlb_teams"
            description = "d"
            inputSchema: ClassVar[dict[str, Any]] = {"type": "object"}

        return [T()]

    async def tools_for_capability(self, capability: str) -> list[str]:
        return ["mlb_teams"]

    async def call_tool(self, name: str, args: Any = None) -> Any:
        return {"ok": name}


@pytest.fixture(autouse=True)
def _fake_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeManager.instances.clear()
    monkeypatch.setattr(pool_mod, "MCPManager", FakeManager)


# ── pool semantics ───────────────────────────────────────────────────────


async def test_pool_shares_identical_scope_and_separates_different() -> None:
    async with MCPSessionPool() as pool:
        a = await pool.get([])  # unscoped → "*"
        b = await pool.get([])
        c = await pool.get(["mlb.reference"])
        assert a is b, "identical scopes must share one subprocess"
        assert c is not a, "different scopes must not share"
        assert len(pool) == 2
    assert all(m.exited for m in FakeManager.instances), "pool close must close every manager"


async def test_runtime_borrows_from_pool_and_does_not_close_it() -> None:
    spec = AgentSpec.model_validate(
        {
            "id": "capped",
            "display_name": "c",
            "system_prompt": "x",
            "tools": {"mcp_capabilities": ["ref.teams"]},
        }
    )

    class _NeverCalled:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            raise AssertionError("no model call expected")

    async with MCPSessionPool() as pool:
        async with AgentRuntime(spec, provider=_NeverCalled(), workspace=WS, pool=pool) as rt:
            assert rt.harness is not None and "mlb_teams" in rt.harness.tools
        shared = FakeManager.instances[0]
        assert not shared.exited, "runtime must not close a pool-owned manager"
    assert shared.exited, "pool close must close the shared manager"


# ── shared team budget (§16.1) ───────────────────────────────────────────


class ScriptedProvider:
    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


def _text(text: str, cost: float = 0.001) -> ModelReply:
    return ModelReply(text=text, model="fake", tokens_in=50, tokens_out=10, cost_usd=cost)


def _delegate(agent_id: str) -> ModelReply:
    return ModelReply(
        text="",
        model="fake",
        tokens_in=50,
        tokens_out=10,
        cost_usd=0.001,
        tool_calls=(ToolCallRequest(id="d", name=agent_id, arguments={"task": "t"}),),
    )


def _spec(id_: str, **over: Any) -> AgentSpec:
    base: dict[str, Any] = {"id": id_, "display_name": id_, "system_prompt": "x"}
    base.update(over)
    return AgentSpec.model_validate(base)


async def test_team_run_shares_one_budget() -> None:
    """Root cost must include the delegate's spend — one ceiling for the whole run."""
    sub_provider = ScriptedProvider(_text("sub answer"))
    orch_provider = ScriptedProvider(_delegate("sub_agent"), _text("final"))

    async with (
        AgentRuntime(_spec("sub_agent"), provider=sub_provider, workspace=WS) as sub,
        AgentRuntime(
            _spec("orch", can_delegate_to=["sub_agent"]), provider=orch_provider, workspace=WS, delegates=[sub]
        ) as orch,
    ):
        res = await orch.run("q")

    assert res.stop_reason == "done"
    # 2 orchestrator calls + 1 delegate call, ALL on the same budget
    assert res.cost_usd == pytest.approx(0.003)


async def test_delegate_spend_trips_the_callers_ceiling() -> None:
    """When the delegate exhausts the shared budget, the CALLER stops too."""
    ws = Workspace(tenant_id="t", workspace_id="w", budgets=Budgets(per_run_usd=0.0015))
    sub_provider = ScriptedProvider(_text("sub answer"))  # charges to 0.002 → over
    orch_provider = ScriptedProvider(_delegate("sub_agent"), _text("never"))

    async with (
        AgentRuntime(_spec("sub_agent"), provider=sub_provider, workspace=ws) as sub,
        AgentRuntime(
            _spec("orch", can_delegate_to=["sub_agent"]), provider=orch_provider, workspace=ws, delegates=[sub]
        ) as orch,
    ):
        res = await orch.run("q")

    assert res.stop_reason == "budget_exhausted"
    assert orch_provider.calls == 1  # the second orchestrator call was refused
    assert res.cost_usd == pytest.approx(0.002)  # total team spend, one ledger
