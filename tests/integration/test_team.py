"""M0.8 — the team over the real data plane.

The offline-ish tests spawn the local sportsdata-mcp subprocess (skip when absent,
e.g. CI) but hit no upstream APIs — capability resolution is computed server-side.
The end-to-end test needs a real model key (and real upstreams) and is ``live``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.agents.runtime import AgentRuntime, open_team
from sportsdata_agents.mcp.pool import MCPSessionPool
from sportsdata_agents.models.gateway import ModelGateway, UsageEvent
from sportsdata_agents.workspace import Budgets, Workspace

MCP_BIN = Path("/Users/danieltomaro/Documents/Projects/sportsdata-mcp/.venv/bin/sportsdata-mcp")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MCP_BIN.exists(), reason="local sportsdata-mcp binary not available"),
]

WS = Workspace(tenant_id="t", workspace_id="w")


class _NeverCalledProvider:
    """For tests that must not reach a model."""

    async def complete(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("model should not have been called")


async def test_specialist_capabilities_resolve_against_real_catalogue() -> None:
    """The bundled specs' capability tags must resolve to real tools (deferred M0.6 check)."""
    specs = load_builtin_specs()
    for agent_id in ("odds_specialist", "stats_specialist"):
        async with AgentRuntime(
            specs[agent_id],
            provider=_NeverCalledProvider(),
            workspace=WS,
            mcp_command=[str(MCP_BIN)],
        ) as rt:
            assert rt.harness is not None
            tool_names = set(rt.harness.tools)
            assert len(tool_names) > 10, f"{agent_id} resolved suspiciously few tools: {tool_names}"
    # spot-checks: capability filtering really constrained the catalogue
    async with AgentRuntime(
        specs["stats_specialist"], provider=_NeverCalledProvider(), workspace=WS, mcp_command=[str(MCP_BIN)]
    ) as rt:
        assert rt.harness is not None
        names = set(rt.harness.tools)
        assert "mlb_teams" in names  # ref.teams
        assert "mlb_boxscore" in names  # sport.match_boxscore
        assert not any("betting" in n or n.startswith("datagolf_outrights") for n in names)


async def test_team_shares_one_subprocess_for_identical_scopes() -> None:
    """Both specialists are unscoped ('*') → the pool must spawn exactly ONE server."""
    specs = load_builtin_specs()
    async with (
        MCPSessionPool(command=[str(MCP_BIN)]) as pool,
        open_team(specs, "orchestrator", provider=_NeverCalledProvider(), workspace=WS, pool=pool) as team,
    ):
        assert team.harness is not None
        assert len(pool) == 1, f"expected 1 shared subprocess, pool has {len(pool)}"


# Provider-agnostic live E2E: uses whichever key is present. A FREE-tier key works —
# e.g. Google AI Studio (GEMINI_API_KEY) or Groq (GROQ_API_KEY) cost $0.
_LIVE_PROVIDERS: list[tuple[str, str | None]] = [
    ("ANTHROPIC_API_KEY", None),  # None = use the policy's default tiers
    ("OPENROUTER_API_KEY", "openrouter/openai/gpt-4o-mini"),  # one key, many models
    ("GEMINI_API_KEY", "gemini/gemini-2.0-flash"),
    ("GROQ_API_KEY", "groq/llama-3.3-70b-versatile"),
    ("OPENAI_API_KEY", "openai/gpt-4o-mini"),
]


def _live_model_override() -> str | None | object:
    for env_name, model in _LIVE_PROVIDERS:
        if os.environ.get(env_name):
            return model
    return _NO_KEY


_NO_KEY = object()


def _live_workspace() -> Workspace:
    override = _live_model_override()
    model_tiers = (
        {"fast": override, "balanced": override, "strong": override} if isinstance(override, str) else {}
    )
    return Workspace(
        tenant_id="t",
        workspace_id="w",
        budgets=Budgets(per_run_usd=0.50, timeout_seconds=180),
        model_tiers=model_tiers,
    )


@pytest.mark.live
@pytest.mark.skipif(
    _live_model_override() is _NO_KEY,
    reason="no model API key set (any of ANTHROPIC/OPENROUTER/GEMINI/GROQ/OPENAI _API_KEY)",
)
async def test_specialist_returns_typed_output_live() -> None:
    """M0.9 exit gate: a specialist answers a scoped question via the real MCP with a
    VALIDATED typed output (StatsAnswer), sources included."""
    from sportsdata_agents.agents.outputs import StatsAnswer

    specs = load_builtin_specs()
    async with AgentRuntime(
        specs["stats_specialist"], provider=ModelGateway(), workspace=_live_workspace(), mcp_command=[str(MCP_BIN)]
    ) as rt:
        res = await rt.run("Using MLB data: which team does Aaron Judge play for? Cite the tool you used.")

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason} — {res.output[:200]}"
    assert isinstance(res.parsed, StatsAnswer), f"typed parse failed; raw: {res.output[:300]}"
    assert "yankee" in res.parsed.answer.lower()
    assert res.parsed.sources, "typed answer carried no sources"


@pytest.mark.live
@pytest.mark.skipif(
    _live_model_override() is _NO_KEY,
    reason="no model API key set (any of ANTHROPIC/OPENROUTER/GEMINI/GROQ/OPENAI _API_KEY)",
)
async def test_team_end_to_end_real_model() -> None:
    """P0's headline: orchestrator delegates to a specialist over the real MCP with a
    real model, and the answer comes back grounded + metered."""
    events: list[UsageEvent] = []
    gateway = ModelGateway(usage_sink=events.append)

    override = _live_model_override()
    model_tiers: dict[str, str] = {}
    if isinstance(override, str):  # pin every tier to the available provider's model
        model_tiers = {"fast": override, "balanced": override, "strong": override}
    ws = Workspace(
        tenant_id="t",
        workspace_id="w",
        budgets=Budgets(per_run_usd=0.50, timeout_seconds=180),
        model_tiers=model_tiers,
    )

    specs = load_builtin_specs()
    async with open_team(
        specs, "orchestrator", provider=gateway, workspace=ws, mcp_command=[str(MCP_BIN)]
    ) as team:
        res = await team.run("Using MLB data: which team is Aaron Judge on right now? One sentence.")

    assert res.stop_reason == "done", f"stopped early: {res.stop_reason} — {res.output[:200]}"
    assert "yankee" in res.output.lower(), res.output
    # the orchestrator actually delegated (a tool message names a specialist)
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert tool_msgs, "no delegation happened"
    # and every model call was metered — either with a real cost, or honestly flagged
    # cost_known=False when litellm has no pricing for the provider's model (§16.1)
    assert events and all(e.tenant_id == "t" for e in events)
    assert res.cost_usd > 0 or all(not e.cost_known for e in events)
