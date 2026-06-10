"""Golden eval cases (`-m eval`) — graded deterministically, run with a real model.

The seed of the M2.4 eval harness: each case is a real question through the real
stack, graded on factual accuracy + grounding (not style). Excluded from CI's
default run; execute locally with `pytest -m eval` (needs a model key — free
Gemini/Groq tiers work — and the local sportsdata-mcp binary).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.agents.runtime import AgentRuntime, open_team
from sportsdata_agents.gateway.service import detect_tier_overrides, has_model_key
from sportsdata_agents.models.gateway import ModelGateway
from sportsdata_agents.workspace import Budgets, Workspace

MCP_BIN = Path("/Users/danieltomaro/Documents/Projects/sportsdata-mcp/.venv/bin/sportsdata-mcp")

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(not MCP_BIN.exists(), reason="local sportsdata-mcp binary not available"),
    pytest.mark.skipif(not has_model_key(), reason="no model API key set (free Gemini/Groq tiers work)"),
]


def _ws() -> Workspace:
    return Workspace(
        tenant_id="t",
        workspace_id="eval",
        budgets=Budgets(per_run_usd=0.50, timeout_seconds=600),
        model_tiers=detect_tier_overrides(),
    )


async def test_golden_stats_fact_grounded() -> None:
    """Factual accuracy: a current-roster fact, answered from live data, grounded."""
    specs = load_builtin_specs()
    async with open_team(
        specs, "orchestrator", provider=ModelGateway(), workspace=_ws(), mcp_command=[str(MCP_BIN)]
    ) as team:
        res = await team.run("Using MLB data: which team does Aaron Judge play for? One sentence.")

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason}"
    assert "yankee" in res.output.lower(), f"wrong/ungrounded answer: {res.output[:200]}"
    assert res.verified is not False, "answer failed the grounding check"
    assert any(m.get("role") == "tool" for m in res.messages), "no delegation/tool use happened"


async def test_golden_odds_math_exact() -> None:
    """Numeric accuracy: deterministic math must be exact (the tool computes, the
    model narrates) and survive the grounding check."""
    specs = load_builtin_specs()
    async with AgentRuntime(
        specs["odds_specialist"], provider=ModelGateway(), workspace=_ws(), mcp_command=[str(MCP_BIN)]
    ) as rt:
        res = await rt.run("What is the implied probability of decimal odds 2.50? Use your tools.")

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason}"
    assert "40" in res.output or "0.4" in res.output, f"wrong math: {res.output[:200]}"
    assert res.verified is True, f"grounding did not verify: {res.output[:200]}"


# ── M2.4: routing efficiency + quant answer accuracy (live, scheduled) ───


async def test_routing_efficiency_stats_question_goes_to_stats() -> None:
    """Routing eval: a stats question must be delegated to a stats-capable
    specialist, not answered from the orchestrator's imagination (it has no data
    tools, so a non-delegated answer is either a refusal or a fabrication)."""

    class RouteRecorder:
        def __init__(self) -> None:
            self.agents: list[str] = []

        async def on_run_start(self, **kw: object) -> None:
            self.agents.append(str(kw.get("agent")))

        async def on_tool_call(self, **kw: object) -> None: ...

        async def on_run_end(self, **kw: object) -> None: ...

    recorder = RouteRecorder()
    specs = load_builtin_specs()
    async with open_team(
        specs, "orchestrator", provider=ModelGateway(), workspace=_ws(),
        mcp_command=[str(MCP_BIN)], recorder=recorder,
    ) as team:
        res = await team.run("How many home runs has Aaron Judge hit this MLB season?")

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason}"
    delegated = [a for a in recorder.agents if a != "orchestrator"]
    assert delegated, "no delegation happened — the orchestrator answered a data question alone"
    assert any(a in ("stats_specialist", "data_analysis") for a in delegated), (
        f"routed to {delegated} instead of a stats-capable specialist"
    )


async def test_value_finder_agent_math_grounded() -> None:
    """Quant answer accuracy: value_scout must run value_finder and quote ITS edge
    numbers (deterministic math), grounding-verified."""
    specs = load_builtin_specs()
    async with AgentRuntime(
        specs["value_scout"], provider=ModelGateway(), workspace=_ws(), mcp_command=[str(MCP_BIN)]
    ) as rt:
        res = await rt.run(
            "Market: home 1.85, away 2.05 (the full market). My calibrated model says "
            "home 0.58, away 0.42. Which selections are value at a 2% edge threshold?"
        )

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason}"
    assert "home" in res.output.lower(), f"missed the +EV selection: {res.output[:200]}"
    assert "7.3" in res.output, f"edge % not quoted from value_finder: {res.output[:200]}"
    assert res.verified is True, "quant numbers failed grounding"
