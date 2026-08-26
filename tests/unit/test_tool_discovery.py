"""Discovery: reach the data plane without carrying its schemas (§8.2).

These are the deterministic half of Phase 2's gate. They need no model key, no network
and no MCP subprocess — a fake manager stands in for the data plane, so they assert the
MECHANISM: what is reachable, what is refused, and what a bad call answers with.

The other half — whether a model then chooses well from a shortlist — cannot be measured
without a model. That risk is carried by `mcp_discover` being per-agent, so discovery can
be reverted one spec at a time. See evals/golden/retrieval.json for the recall@N score,
which measures whether the right tool reaches the shortlist at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.mcp.discovery import discovery_tools, score_tools

pytestmark = pytest.mark.unit


class FakeManager:
    """The data plane, minus the subprocess. Records what was called."""

    def __init__(self, index: dict[str, list[dict[str, Any]]]) -> None:
        self._index = index
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, args or {}))
        if name == "list_tools_by_capability":
            return {"tools": self._index.get(args["capability"], [])}
        from sportsdata_agents.mcp.manager import is_denied

        if is_denied(name):  # the real manager refuses at call time; so must the fake
            from sportsdata_agents.mcp.manager import ForbiddenToolError

            raise ForbiddenToolError(name)
        return {"ok": name, "args": args}


def _entry(tool: str, provider: str, summary: str = "", required: list[str] | None = None):
    return {"tool": tool, "provider": provider, "summary": summary,
            "args_required": required or []}


INDEX = {
    "sport.prices": [
        _entry("pinnacle_matchup_markets", "pinnacle", "Markets and prices for one matchup.", ["matchup_id"]),
        _entry("betfair_market_prices", "betfair", "Best back and lay prices for a market.", ["market_ids"]),
    ],
    "sport.in_play": [
        _entry("apitennis_livescore", "apitennis", "Live tennis scores in progress."),
    ],
}


def _tools(manager, caps=("sport.prices", "sport.in_play")):
    return {t.name: t for t in discovery_tools(manager, list(caps))}


async def test_an_agent_pays_two_schemas_however_far_the_capabilities_fan_out() -> None:
    """The point of the phase: tool COUNT stops tracking catalogue size."""
    tools = _tools(FakeManager(INDEX))
    assert set(tools) == {"find_data_tools", "call_data_tool"}


async def test_search_finds_a_tool_by_what_it_does_not_by_its_name() -> None:
    tools = _tools(FakeManager(INDEX))
    result = await tools["find_data_tools"].execute({"query": "back and lay prices"})
    assert [m["tool"] for m in result["matches"]][:1] == ["betfair_market_prices"]


async def test_a_search_that_matches_nothing_says_what_can_be_searched() -> None:
    """An empty list reads as 'no such data exists', which is rarely true and leaves the
    model with nowhere to go."""
    tools = _tools(FakeManager(INDEX))
    result = await tools["find_data_tools"].execute({"query": "zzzz nonexistent"})
    assert result["matches"] == []
    assert set(result["granted"]) == {"sport.prices", "sport.in_play"}


async def test_a_discovered_tool_can_be_called_and_carries_its_provenance() -> None:
    manager = FakeManager(INDEX)
    tools = _tools(manager)
    result = await tools["call_data_tool"].execute(
        {"tool_name": "betfair_market_prices", "args": {"market_ids": "1.23"}}
    )
    assert result["data"]["ok"] == "betfair_market_prices"
    # Same envelope a directly-attached tool gets, so a figure can be cited to its
    # source whichever route reached it.
    assert result["_source"]["tool"] == "betfair_market_prices"
    assert result["_source"]["via"] == "call_data_tool"
    assert result["_source"]["fetched_at"]


async def test_a_tool_outside_the_granted_capabilities_is_refused() -> None:
    """THE SECURITY PROPERTY. Without this, call_data_tool is one tool that reaches the
    entire catalogue regardless of what the spec granted — a privilege escalation
    dressed as a convenience."""
    manager = FakeManager(INDEX)
    tools = _tools(manager, caps=["sport.prices"])   # in_play NOT granted
    result = await tools["call_data_tool"].execute({"tool_name": "apitennis_livescore", "args": {}})
    assert "not reachable" in result["error"]
    assert result["granted_capabilities"] == ["sport.prices"]
    # refused before reaching the data plane at all
    assert not [c for c in manager.calls if c[0] == "apitennis_livescore"]


async def test_a_money_verb_tool_is_neither_listed_nor_callable() -> None:
    """The advisory invariant must not depend on how a tool name arrives. Discovery is a
    new route to the catalogue, so it gets the same deny-filter — enforced twice: the
    name is never shown, and MCPManager.call_tool refuses it even if hallucinated."""
    index = {"sport.prices": INDEX["sport.prices"] + [_entry("sportsbet_place_bet", "sportsbet", "Place a bet.")]}
    manager = FakeManager(index)
    tools = _tools(manager, caps=["sport.prices"])

    listed = await tools["find_data_tools"].execute({"query": "bet"})
    assert "sportsbet_place_bet" not in [m["tool"] for m in listed["matches"]]

    called = await tools["call_data_tool"].execute({"tool_name": "sportsbet_place_bet", "args": {}})
    assert "not reachable" in called["error"]


async def test_a_failed_call_answers_with_the_arguments_it_needed() -> None:
    """Malformed arguments are the EXPECTED failure mode here — a discovered tool arrives
    as prose, not a validated schema. A bare error would strand the model."""
    class Failing(FakeManager):
        async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
            if name == "list_tools_by_capability":
                return await super().call_tool(name, args)
            raise RuntimeError("missing required argument: matchup_id")

    tools = _tools(Failing(INDEX))
    result = await tools["call_data_tool"].execute({"tool_name": "pinnacle_matchup_markets", "args": {}})
    assert "matchup_id" in result["error"]
    assert result["args_required"] == ["matchup_id"]


async def test_the_catalogue_is_resolved_once_per_run() -> None:
    """Discovery must not re-walk the catalogue on every search, or it trades token cost
    for latency and undoes the point."""
    manager = FakeManager(INDEX)
    tools = _tools(manager)
    await tools["find_data_tools"].execute({"query": "prices"})
    await tools["find_data_tools"].execute({"query": "live"})
    await tools["call_data_tool"].execute({"tool_name": "betfair_market_prices", "args": {}})
    lookups = [c for c in manager.calls if c[0] == "list_tools_by_capability"]
    assert len(lookups) == 2  # one per granted capability, not per search


def test_ranking_prefers_the_name_then_the_provider_then_the_summary() -> None:
    """The eval scores this function; this pins the ordering it depends on."""
    tools = [
        _entry("alpha_tool", "alpha", "mentions prices once"),
        _entry("prices_tool", "beta", "unrelated text"),
    ]
    assert score_tools("prices", tools, 5)[0]["tool"] == "prices_tool"
