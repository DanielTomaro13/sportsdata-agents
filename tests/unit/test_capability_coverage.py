"""Guard the data-plane leverage: agents must keep using a broad slice of the MCP
capability catalogue, and the racing / prediction-market surfaces stay covered."""

from __future__ import annotations

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.tools.builder import capability_labels

pytestmark = pytest.mark.unit


def _used_capabilities() -> set[str]:
    used: set[str] = set()
    for spec in load_builtin_specs().values():
        used.update(spec.tools.mcp_capabilities)
    return used


def test_racing_and_prediction_surfaces_are_leveraged() -> None:
    used = _used_capabilities()
    assert {c for c in used if c.startswith("racing.")}, "no agent uses any racing.* capability"
    assert {c for c in used if c.startswith("prediction.")}, "no agent uses any prediction.* capability"


def test_broad_catalogue_coverage() -> None:
    catalogue = set(capability_labels())
    used = _used_capabilities() & catalogue
    # regression guard: we deliberately leverage a broad slice of the data plane.
    assert len(used) >= 30, f"only {len(used)}/{len(catalogue)} capabilities used — leverage regressed"


def test_new_specialists_load_and_are_pro_only() -> None:
    from sportsdata_agents.licensing.entitlements import entitlements_for_tier

    specs = load_builtin_specs()
    for agent_id in ("racing_analyst", "prediction_market_analyst"):
        assert agent_id in specs, f"{agent_id} spec did not load"
        assert specs[agent_id].plane == "product"

    plus = entitlements_for_tier("plus")
    assert plus.agents is not None  # plus has a restricted roster
    assert "racing_analyst" not in plus.agents  # full roster is a Pro feature
    pro = entitlements_for_tier("pro")
    assert pro.allows_agent("racing_analyst") and pro.allows_agent("prediction_market_analyst")


def test_orchestrator_can_reach_the_new_specialists() -> None:
    orch = load_builtin_specs()["orchestrator"]
    assert "racing_analyst" in orch.can_delegate_to
    assert "prediction_market_analyst" in orch.can_delegate_to
