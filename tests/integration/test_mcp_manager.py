"""M0.4 — MCPManager against a real local sportsdata-mcp subprocess.

These spawn the **sibling repo's** server binary over stdio. They skip when it isn't
present (e.g. CI, which can't install the private data plane). Scoping + catalogue +
capability tests are offline (no upstream HTTP — the meta-tools compute locally);
the single upstream-data test is marked ``live``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.mcp.manager import ForbiddenToolError, MCPManager

MCP_BIN = Path("/Users/danieltomaro/Documents/Projects/sportsdata-mcp/.venv/bin/sportsdata-mcp")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MCP_BIN.exists(), reason="local sportsdata-mcp binary not available"),
]


def _manager(groups: list[str]) -> MCPManager:
    return MCPManager(groups=groups, command=[str(MCP_BIN)])


async def test_scoped_session_registers_only_its_groups() -> None:
    async with _manager(["mlb.reference"]) as m:
        names = await m.tool_names()
    assert "mlb_teams" in names
    assert "mlb_player_search" in names
    # out-of-scope groups are absent — least privilege holds
    assert not any(n.startswith(("openf1_", "tab_", "sportsbet_")) for n in names)
    # meta-tools are always on
    assert "list_tools_by_capability" in names


async def test_deny_filter_holds_end_to_end() -> None:
    async with _manager(["betfair.exchange", "betfair.inplay"]) as m:
        names = await m.tool_names()
        # the strict filter hides the read-only cashout feed
        assert "betfair_cashout" not in names
        assert "betfair_market_prices" in names
        # calling a denied name is refused before any subprocess traffic
        with pytest.raises(ForbiddenToolError):
            await m.call_tool("betfair_cashout", {"marketIds": ["1.1"]})


async def test_capability_lookup_is_cross_provider() -> None:
    async with _manager(["mlb.reference", "openf1.reference"]) as m:
        tools = await m.tools_for_capability("ref.players")
    assert "mlb_player_search" in tools
    assert "openf1_drivers" in tools


@pytest.mark.live
async def test_real_data_roundtrip() -> None:
    """The spine works: spawn → scope → call a real tool → typed payload back."""
    async with _manager(["mlb.reference"]) as m:
        payload = await m.call_tool("mlb_teams", {"sportId": 1})
    assert isinstance(payload, dict) and "teams" in payload
    teams = payload["teams"]
    assert len(teams) == 30
    assert {"id", "name", "abbreviation"} <= set(teams[0].keys())
