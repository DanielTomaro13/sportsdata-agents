"""Multi-call fetchers (M2.1+): providers whose prices need a discovery step.

Most feeds are one tool call; these books split list and prices across endpoints,
so a fetcher composes the calls and returns ONE payload for the normalizer. Same
contract as everything else in the worker: deterministic, no LLM, failures raise
and are isolated per-feed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per-cycle caps: discovery lists can be long; a feed is a snapshot, not a crawl.
MAX_EVENTS_PER_CYCLE = 12


async def fetch_pinnacle_league(manager: Any, *, league_id: int) -> dict[str, Any]:
    """Pinnacle: matchups list carries no prices — fetch straight markets per matchup."""
    matchups = await manager.call_tool("pinnacle_league_matchups", {"leagueId": league_id})
    matchups = [m for m in (matchups or []) if isinstance(m, dict) and m.get("hasMarkets") and not m.get("isLive")]
    markets: dict[str, Any] = {}
    for matchup in matchups[:MAX_EVENTS_PER_CYCLE]:
        try:
            markets[str(matchup["id"])] = await manager.call_tool(
                "pinnacle_matchup_markets", {"matchupId": matchup["id"]}
            )
        except Exception as e:  # one matchup's markets failing must not sink the cycle
            logger.warning("pinnacle matchup %s markets failed: %s", matchup.get("id"), e)
    return {"matchups": matchups, "markets": markets}


async def fetch_pointsbet_competition(manager: Any, *, competition_key: int) -> dict[str, Any]:
    """PointsBet: the competition list inlines only prop markets — Match Result
    lives in the per-event detail (~5MB each; the feed's interval is set accordingly)."""
    listing = await manager.call_tool("pointsbet_competition_events", {"competitionKey": competition_key})
    details: list[Any] = []
    for event in (listing.get("events") or [])[:MAX_EVENTS_PER_CYCLE]:
        key = event.get("key")
        if key is None:
            continue
        try:
            details.append(await manager.call_tool("pointsbet_event", {"eventKey": key}))
        except Exception as e:
            logger.warning("pointsbet event %s detail failed: %s", key, e)
    return {"events": details}


# Trimmed sections: the default set returns full price ladders and trips the
# exchange's TOO_MUCH_DATA fault even for a handful of events.
BETFAIR_TYPES = [
    "MARKET_STATE",
    "MARKET_DESCRIPTION",
    "EVENT",
    "RUNNER_DESCRIPTION",
    "RUNNER_EXCHANGE_PRICES_BEST",
]
BETFAIR_CHUNK = 2  # events per byevent call


async def fetch_betfair_event_type(manager: Any, *, event_type_id: int) -> dict[str, Any]:
    """Betfair exchange: navigation graph → match nodes (named "X v Y") → markets+prices,
    fetched in small chunks with trimmed sections (the exchange faults on big asks)."""
    nav = await manager.call_tool("betfair_navigation", {"nodeIds": [f"EVENT_TYPE:{event_type_id}"]})
    ids: list[str] = []
    for node in nav.get("nodes") or []:
        name = str(node.get("name", ""))
        node_id = str(node.get("nodeId", ""))
        if " v " in name and ":" in node_id:
            numeric = node_id.split(":", 1)[1]
            if numeric not in ids:
                ids.append(numeric)
    merged: dict[str, Any] = {"eventTypes": [{"eventNodes": []}]}
    ids = ids[:MAX_EVENTS_PER_CYCLE]
    for i in range(0, len(ids), BETFAIR_CHUNK):
        chunk = ids[i : i + BETFAIR_CHUNK]
        try:
            out = await manager.call_tool(
                "betfair_markets_by_event", {"eventIds": chunk, "types": BETFAIR_TYPES}
            )
            for event_type in (out.get("eventTypes") or []) if isinstance(out, dict) else []:
                merged["eventTypes"][0]["eventNodes"].extend(event_type.get("eventNodes") or [])
        except Exception as e:  # one chunk failing must not sink the cycle
            logger.warning("betfair chunk %s failed: %s", chunk, e)
    return merged
