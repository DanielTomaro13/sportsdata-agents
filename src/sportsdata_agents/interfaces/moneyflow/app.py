"""FastAPI app: race list + live market proxy against Betfair's public API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

_AK = "nzIFcwyWhrlwYMrh"  # Betfair's public web app key (unauthenticated)
_NAV = "https://scan-inbf.betfair.com.au/www/sports/navigation/v2/graph/bynode"
_ERO = "https://ero.betfair.com.au/www/sports/exchange/readonly/v1/bymarket"
# horses / greyhounds / harness
_RACING_EVENT_TYPES = ("7", "4339", "61420")
_CODE_LABEL = {"7": "horses", "4339": "greyhounds", "61420": "harness"}
_AU_NZ = ("AU", "NZ")
_TYPES = ",".join([
    "MARKET_STATE", "MARKET_DESCRIPTION", "EVENT",
    "RUNNER_DESCRIPTION", "RUNNER_STATE", "RUNNER_EXCHANGE_PRICES_BEST",
])

app = FastAPI(title="moneyflow")


async def _get_json(url: str, params: dict[str, str]) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params={**params, "_ak": _AK},
                                headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.json()


@app.get("/api/races")
async def races() -> list[dict[str, Any]]:
    """Upcoming AU/NZ races across the three codes, soonest first.

    The nav graph hangs WIN markets off RACE nodes with the race name as
    the market name; the EVENT node carries venue + countryCode."""
    out: list[dict[str, Any]] = []
    for code in _RACING_EVENT_TYPES:
        try:
            graph = await _get_json(_NAV, {
                "nodeIds": f"EVENT_TYPE:{code}",
                "attachments": "MENU,EVENT,MARKET",
                "maxOutDistance": "5",
            })
        except httpx.HTTPError as exc:
            logger.warning("nav fetch failed for %s: %s", code, exc)
            continue
        events: dict[str, dict[str, Any]] = {}
        for node in graph.get("nodes") or []:
            info = node.get("eventInfo")
            if info and str(info.get("countryCode", "")) in _AU_NZ:
                events[str(info.get("eventId"))] = info
        for node in graph.get("nodes") or []:
            market = node.get("marketInfo")
            if not market or str(market.get("marketType", "")) != "WIN":
                continue
            info = events.get(str(market.get("eventId")))
            if info is None:
                continue
            out.append({
                "marketId": market.get("marketId"),
                "venue": info.get("venue") or info.get("name"),
                "country": info.get("countryCode"),
                "code": _CODE_LABEL[code],
                "race": market.get("marketName"),  # leads with "R<n>"
                "start": market.get("marketTime"),
                "inplay": bool(market.get("inplay")),
            })
    out.sort(key=lambda r: str(r.get("start") or ""))
    return out


@app.get("/api/market/{market_id}")
async def market(market_id: str) -> dict[str, Any]:
    """One market's live board, flattened for the page."""
    try:
        data = await _get_json(_ERO, {"marketIds": market_id, "types": _TYPES})
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
