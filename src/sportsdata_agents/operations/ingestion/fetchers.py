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


async def fetch_fanduel_event_pages(manager: Any, *, page_id: str) -> dict[str, Any]:
    """FanDuel US sportsbook: a content page's markets reference event ids, but
    MONEY_LINE lives on the per-event page — fetch those."""
    page = await manager.call_tool(
        "fanduel_sb_call", {"operation": "content_page", "query_params": {"customPageId": page_id}}
    )
    event_ids: list[str] = []
    for market in ((page.get("attachments") or {}).get("markets") or {}).values():
        event_id = str(market.get("eventId", ""))
        if event_id and event_id not in event_ids:
            event_ids.append(event_id)
    pages: list[Any] = []
    for event_id in event_ids[:MAX_EVENTS_PER_CYCLE]:
        try:
            detail = await manager.call_tool(
                "fanduel_sb_call", {"operation": "event_page", "query_params": {"eventId": int(event_id)}}
            )
            pages.append(detail.get("attachments") or {})
        except Exception as e:
            logger.warning("fanduel event %s page failed: %s", event_id, e)
    return {"pages": pages}


async def fetch_fanduel_races(manager: Any, *, max_races: int = 10) -> dict[str, Any]:
    """FanDuel Racing/TVG: featured races (curated, near post) → full race cards
    with current odds."""
    featured = await manager.call_tool("fanduel_racing_call", {"operation": "getFeaturedRaces"})
    races_meta = ((featured.get("data") or {}).get("races")) or []
    races: list[Any] = []
    for meta in races_meta[:max_races]:
        track, number = meta.get("trackCode"), meta.get("raceNumber")
        if not track or number is None:
            continue
        try:
            card = await manager.call_tool(
                "fanduel_racing_call",
                {"operation": "getRace", "variables": {"trackCode": str(track), "raceNumber": str(number)}},
            )
            race = (card.get("data") or {}).get("race")
            if race:
                race.setdefault("trackName", meta.get("trackName"))
                races.append(race)
        except Exception as e:
            logger.warning("fanduel race %s-%s failed: %s", track, number, e)
    return {"races": races}
