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
