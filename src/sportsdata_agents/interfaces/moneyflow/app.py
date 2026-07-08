"""FastAPI app: sport/event/market browser + live market proxy, whole exchange."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

_AK = "nzIFcwyWhrlwYMrh"  # Betfair's public web app key (unauthenticated)
_NAV = "https://scan-inbf.betfair.com.au/www/sports/navigation/v2/graph/bynode"
_ERO = "https://ero.betfair.com.au/www/sports/exchange/readonly/v1/bymarket"
# The nav graph has no discoverable root, so candidate event types are
# curated; /api/sports validates each live and only returns the ones the
# exchange actually answers for.
_EVENT_TYPE_CANDIDATES = (
    "7", "4339",  # horse racing, greyhound racing (racing UX: one row per WIN market)
    "1", "2", "4", "5", "1477", "61420",  # soccer, tennis, cricket, union, league, AFL
    "7522", "7511", "7524", "6423",  # basketball, baseball, ice hockey, american football
    "3", "6", "26420387", "3503", "6422", "8", "11",  # golf, boxing, mma, darts, snooker, motorsport, cycling
    "27454571", "2378961", "10",  # esports, politics, specials
)
_RACING = {"7": "🐎", "4339": "🐕"}
_TYPES = ",".join([
    "MARKET_STATE", "MARKET_DESCRIPTION", "EVENT",
    "RUNNER_DESCRIPTION", "RUNNER_STATE", "RUNNER_EXCHANGE_PRICES_BEST",
])
_MAX_ITEMS = 150  # soonest-first cap per sport (soccer alone has thousands)
_MAX_MARKETS_PER_EVENT = 40

app = FastAPI(title="moneyflow")
_sports_cache: tuple[float, list[dict[str, Any]]] | None = None


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> Any:
    resp = await client.get(url, params={**params, "_ak": _AK},
                            headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json()


async def _nav(client: httpx.AsyncClient, type_id: str, depth: str,
               attachments: str = "MENU,EVENT,MARKET") -> Any:
    return await _get_json(client, _NAV, {
        "nodeIds": f"EVENT_TYPE:{type_id}",
        "attachments": attachments,
        "maxOutDistance": depth,
    })


@app.get("/api/sports")
async def sports() -> list[dict[str, Any]]:
    """Live-validated event types, cached 10 minutes."""
    global _sports_cache
    if _sports_cache and time.monotonic() - _sports_cache[0] < 600:
        return _sports_cache[1]

    async def probe(client: httpx.AsyncClient, type_id: str) -> dict[str, Any] | None:
        try:
            graph = await _nav(client, type_id, "1", attachments="MENU")
        except httpx.HTTPError:
            return None
        for node in graph.get("nodes") or []:
            if node.get("nodeType") == "EVENT_TYPE":
                return {"id": type_id, "name": str(node.get("name", type_id)),
                        "racing": type_id in _RACING}
        return None

    async with httpx.AsyncClient(timeout=15) as client:
        found = await asyncio.gather(*(probe(client, t) for t in _EVENT_TYPE_CANDIDATES))
    out = [s for s in found if s]
    _sports_cache = (time.monotonic(), out)
    return out


@app.get("/api/list/{type_id}")
async def listing(type_id: str) -> list[dict[str, Any]]:
    """Browseable items for one sport, soonest first.

    Racing: one item per WIN market (the race), any country. Everything
    else: one item per event, carrying every market the nav exposes so
    the page can offer a market picker."""
    # racing hangs WIN markets shallow; team sports need two more hops
    # (EVENT_TYPE -> group -> competition -> event -> market) to reach the
    # per-match market set
    depth = "5" if type_id in _RACING else "7"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            graph = await _nav(client, type_id, depth)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    nodes = graph.get("nodes") or []
    events = {str(n["eventInfo"].get("eventId")): n["eventInfo"]
              for n in nodes if n.get("eventInfo")}

    if type_id in _RACING:
        out = []
        for node in nodes:
            market = node.get("marketInfo")
            if not market or str(market.get("marketType", "")) != "WIN":
                continue
            info = events.get(str(market.get("eventId"))) or {}
            country = str(info.get("countryCode") or "")
            out.append({
                "marketId": market.get("marketId"),
                "label": f"{info.get('venue') or info.get('name', '?')}"
                         + (f" ({country})" if country else ""),
                "sub": market.get("marketName"),  # leads with "R<n>"
                "start": market.get("marketTime"),
                "inplay": bool(market.get("inplay")),
                "markets": [],
            })
        out.sort(key=lambda r: str(r.get("start") or ""))
        return out[:_MAX_ITEMS]

    by_event: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        market = node.get("marketInfo")
        if not market or not market.get("marketId"):
            continue
        event_id = str(market.get("eventId"))
        if event_id in events:
            by_event.setdefault(event_id, []).append(market)
    out = []
    for event_id, markets in by_event.items():
        info = events[event_id]
        # match odds (or its regular-time variant) fronts the picker
        markets.sort(key=lambda m: (str(m.get("marketType")) != "MATCH_ODDS",
                                    str(m.get("marketName", ""))))
        markets = markets[:_MAX_MARKETS_PER_EVENT]
        country = str(info.get("countryCode") or "")
        # the event's openDate goes stale on long-running outrights; the
        # earliest market time is what "next up" actually means
        starts = sorted(str(m.get("marketTime")) for m in markets if m.get("marketTime"))
        out.append({
            "marketId": markets[0].get("marketId"),
            "label": str(info.get("name", "?")),
            "sub": (markets[0].get("marketName") or "")
                   + (f" · {country}" if country else "")
                   + (f" · {len(markets)} markets" if len(markets) > 1 else ""),
            "start": starts[0] if starts else info.get("openDate"),
            "inplay": any(m.get("inplay") for m in markets),
            "markets": [{"marketId": m.get("marketId"), "name": m.get("marketName")}
                        for m in markets],
        })
    out.sort(key=lambda r: str(r.get("start") or ""))
    return out[:_MAX_ITEMS]


@app.get("/api/market/{market_id}")
async def market(market_id: str) -> dict[str, Any]:
    """One market's live board, flattened for the page."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            data = await _get_json(client, _ERO, {"marketIds": market_id, "types": _TYPES})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    for event_type in data.get("eventTypes") or []:
        for event_node in event_type.get("eventNodes") or []:
            for market_node in event_node.get("marketNodes") or []:
                if str(market_node.get("marketId")) != market_id:
                    continue
                return _flatten(market_node, event_node.get("event") or {})
    raise HTTPException(status_code=404, detail="market not found")


def _flatten(node: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    state = node.get("state") or {}
    description = node.get("description") or {}
    runners: list[dict[str, Any]] = []
    for runner in node.get("runners") or []:
        runner_state = runner.get("state") or {}
        if str(runner_state.get("status", "ACTIVE")) not in ("ACTIVE", "WINNER"):
            continue
        exchange = runner.get("exchange") or {}
        backs = exchange.get("availableToBack") or []
        lays = exchange.get("availableToLay") or []
        runners.append({
            "name": str((runner.get("description") or {}).get("runnerName", "?")),
            "back": backs[0].get("price") if backs else None,
            "backSize": backs[0].get("size") if backs else None,
            "lay": lays[0].get("price") if lays else None,
            "laySize": lays[0].get("size") if lays else None,
            "lastTraded": runner_state.get("lastPriceTraded"),
            "matched": runner_state.get("totalMatched") or 0.0,
            "status": runner_state.get("status"),
        })
    runners.sort(key=lambda r: (r["lastTraded"] or r["back"] or 999.0))
    return {
        "marketId": node.get("marketId"),
        "eventName": event.get("eventName"),
        "marketName": description.get("marketName"),
        "start": description.get("marketTime"),
        "inplay": bool(state.get("inplay")),
        "status": state.get("status"),
        "totalMatched": state.get("totalMatched") or 0.0,
        "totalAvailable": state.get("totalAvailable") or 0.0,
        "runners": runners,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (Path(__file__).parent / "page.html").read_text()
