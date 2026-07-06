"""Multi-call fetchers (M2.1+): providers whose prices need a discovery step.

Most feeds are one tool call; these books split list and prices across endpoints,
so a fetcher composes the calls and returns ONE payload for the normalizer. Same
contract as everything else in the worker: deterministic, no LLM, failures raise
and are isolated per-feed.
"""

from __future__ import annotations

import logging
import os
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
            # the page slug (nba, pga, ncaaf) is the only sport label these carry
            pages.append({**(detail.get("attachments") or {}), "sport": page_id})
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


# ─── discovery fetchers: capture EVERYTHING a provider currently prices ──
# Each walks the book's own discovery route, so coverage tracks the book, not a
# hand-curated id list. Sport labels ride inside the combined payload (the "_sport"
# convention) — slugs are each book's own naming; canonical cross-book sport ids
# belong to the event-resolution milestone.

ROTATE_CAP = 40  # markets-bearing calls per cycle for the N+1 providers
# Windows derive from WALL-CLOCK time, not process state: the documented operating
# mode is cron'd `agents ingest --once`, where every invocation is a fresh process —
# an in-memory offset would re-fetch the same first window forever.
ROTATION_EPOCH_S = 600  # one window step per 10 minutes


def _take_rotating(name: str, items: list[Any], cap: int) -> list[Any]:
    """A rotating window over a long discovery list: the offset advances with wall
    clock, so the whole board gets covered across cycles AND across processes."""
    import time

    if len(items) <= cap:
        return items
    step = int(time.time() // ROTATION_EPOCH_S)
    offset = (step * cap) % len(items)
    window = items[offset : offset + cap]
    if len(window) < cap:
        window += items[: cap - len(window)]
    return window


def _slug(name: str) -> str:
    return "_".join(str(name).strip().lower().split())


async def fetch_unibet_all(manager: Any) -> dict[str, Any]:
    """Kambi: group.json → every sport termKey → TWO listView calls per sport:
    matches.json (fixtures + inline offers) and competitions.json (outrights —
    premiership winners and other futures live ONLY there; B9)."""
    root = await manager.call_tool("unibet_kambi_call", {"operation": "group"})
    group = root.get("group") or root
    sports: list[str] = []
    for g in group.get("groups", []) or []:
        term = g.get("termKey")
        if term and (g.get("boCount") or 0) > 0:
            sports.append(str(term))
    out: list[dict[str, Any]] = []
    for term in sports:
        for operation in ("sport_matches", "sport_competitions"):
            try:
                payload = await manager.call_tool(
                    "unibet_kambi_call", {"operation": operation, "path_params": {"sport": term}}
                )
                out.append({"sport": term, "payload": payload})
            except Exception as e:
                logger.warning("unibet sport %s (%s) failed: %s", term, operation, e)
    return {"sports": out}


# FALLBACK ONLY — categories are discovered live via the SportingCategories
# GraphQL op (all 26 with UUIDs); this snapshot (documentation/Entain.md) covers
# gateway outages. NOVELTY and POLITICS excluded — not sports.
ENTAIN_SPORT_CATEGORIES: dict[str, str] = {
    "american_football": "a19fe930-3d0c-4f23-9cd4-12132fcc6b0a",
    "australian_rules": "23d497e6-8aab-4309-905b-9421f42c9bc5",
    "baseball": "02721435-4671-4cd0-98f7-15d41ee4103e",
    "basketball": "3c34d075-dc14-436d-bfc4-9272a49c2b39",
    "boxing": "a8217d48-3257-402b-b3b5-9db706fdc1e0",
    "cricket": "94984918-dbac-432b-b420-c219ec9203f4",
    "cycling": "a392063b-7be0-48c8-aa8b-2965e0508dba",
    "darts": "bfe01e5c-664b-4a3a-ba5a-ab15da108c7d",
    "esports": "e89fbf3f-7ed4-47b4-923e-6febc6691ac9",
    "golf": "24d4f135-aeec-4671-a4a3-f4cf555105ab",
    "handball": "b66ac710-c8d3-4cf7-beb3-733f6dff6fa8",
    "ice_hockey": "b7c1f944-d02b-4d9b-b6f3-cb31389cfe36",
    "mma": "2768e4b7-effa-4bd1-929d-2e27f46af4f6",
    "motor_sport": "fff64442-44f4-40d2-b830-5fa9b1bdf9e4",
    "netball": "105f897d-706b-4ff5-a753-80d08004f6d7",
    "pool": "4ed8329a-4f42-46ab-b204-b76fd5e2f37c",
    "rugby_league": "608a1803-45bc-465a-8471-c89dcb68a27d",
    "rugby_union": "33b58e1b-fb14-4cd8-98a7-c03fe6a8ea57",
    "snooker": "9641d713-66ae-4e38-af55-c0249ec15e7a",
    "soccer": "71955b54-62f6-4ac5-abaa-df88cad0aeef",
    "table_tennis": "b92b2d14-10f7-46c7-8655-16eeed36ec4b",
    "tennis": "a0b910b8-85f0-4f6e-821d-c9fd9e3bdf93",
    "volleyball": "c16422dc-2e08-4512-bd42-4ca72a3cdc35",
}


_ENTAIN_NON_SPORTS = ("NOVELTY", "POLITICS")


async def discover_entain_categories(manager: Any) -> dict[str, str]:
    """Entain's SportingCategories GraphQL op lists every sport category with its
    UUID — discovery tracks whatever Entain adds, no code change (verified live
    2026-06-11; UUIDs match the documented snapshot)."""
    payload = await manager.call_tool(
        "entain_graphql_call", {"operation": "SportingCategories", "variables": {}}
    )
    categories = ((payload.get("data") or payload).get("categories")) or []
    out: dict[str, str] = {}
    for cat in categories:
        enum = str(cat.get("category") or "")
        if enum in _ENTAIN_NON_SPORTS or not cat.get("id"):
            continue
        out[_slug(str(cat.get("name") or enum.lower()))] = str(cat["id"])
    return out


async def fetch_entain_all(manager: Any) -> dict[str, Any]:
    """Entain: discovered sport categories → one event-request per category
    (events+markets+prices in bulk)."""
    try:
        categories = await discover_entain_categories(manager)
    except Exception as e:
        logger.warning("entain category discovery failed (using fallback map): %s", e)
        categories = {}
    if not categories:
        categories = dict(ENTAIN_SPORT_CATEGORIES)
    out: list[dict[str, Any]] = []
    for sport, category_id in categories.items():
        try:
            payload = await manager.call_tool(
                "entain_sport_event_request", {"category_ids": [category_id]}
            )
            out.append({"sport": sport, "payload": payload})
        except Exception as e:
            logger.warning("entain category %s failed: %s", sport, e)
    return {"categories": out}


async def fetch_pinnacle_all(manager: Any) -> dict[str, Any]:
    """Pinnacle: sports → every active sport's matchups → straight markets for the
    SOONEST ``ROTATE_CAP`` matchups board-wide (prices live per-matchup; capping by
    start time keeps the actionable board fresh and the call count bounded)."""
    sports = await manager.call_tool("pinnacle_sports", {})
    matchups: list[dict[str, Any]] = []
    for sport in sports or []:
        if not sport.get("matchupCount"):
            continue
        try:
            rows = await manager.call_tool("pinnacle_sport_matchups_all", {"sportId": sport["id"]})
        except Exception as e:
            logger.warning("pinnacle sport %s matchups failed: %s", sport.get("name"), e)
            continue
        for matchup in rows or []:
            if isinstance(matchup, dict) and matchup.get("hasMarkets") and not matchup.get("isLive"):
                matchup["_sport"] = _slug(sport.get("name", "?"))
                matchups.append(matchup)
    matchups.sort(key=lambda m: str(m.get("startTime", "")))
    chosen = matchups[:ROTATE_CAP]
    markets: dict[str, Any] = {}
    for matchup in chosen:
        try:
            markets[str(matchup["id"])] = await manager.call_tool(
                "pinnacle_matchup_markets", {"matchupId": matchup["id"]}
            )
        except Exception as e:
            logger.warning("pinnacle matchup %s markets failed: %s", matchup.get("id"), e)
    return {"matchups": chosen, "markets": markets}


def _walk_sportsbet_nav(node: Any, sport: str | None, found: list[tuple[int, str, str]]) -> None:
    if not isinstance(node, dict):
        return
    id_type = str(node.get("idType", ""))
    name = str(node.get("name", ""))
    next_sport = name if id_type == "class" else sport
    if id_type == "competition" and node.get("id") is not None and next_sport:
        found.append((int(node["id"]), name, _slug(next_sport)))
    for child in node.get("navItems", []) or []:
        _walk_sportsbet_nav(child, next_sport, found)


async def fetch_sportsbet_all(manager: Any) -> dict[str, Any]:
    """Sportsbet: navigation hierarchy → every listed competition → matches AND
    outrights for a rotating window of them (the dated classes route 400s upstream;
    nav is live). Futures competitions (Brownlow, NFL Futures, …) list NO match-type
    events — their priced events come only from the Outrights route (B10)."""
    nav = await manager.call_tool("sportsbet_nav_hierarchy", {})
    comps: list[tuple[int, str, str]] = []
    _walk_sportsbet_nav(nav, None, comps)
    window = _take_rotating("sportsbet_all", comps, ROTATE_CAP)
    out: list[dict[str, Any]] = []
    for comp_id, _comp_name, sport in window:
        for tool in ("sportsbet_competition_matches", "sportsbet_competition_outrights"):
            try:
                payload = await manager.call_tool(tool, {"competitionId": comp_id})
                out.append({"sport": sport, "payload": payload})
            except Exception as e:
                logger.warning("sportsbet competition %s (%s) failed: %s", comp_id, tool, e)
    return {"competitions": out}


async def fetch_tab_all(manager: Any, *, comps_per_cycle: int = 20) -> dict[str, Any]:
    """TAB: sports tree → every (sport, competition) pair → a rotating window of
    competition pages (they're MB-scale, so the window is small and the cadence slow)."""
    sports = await manager.call_tool("tab_sports", {})
    pairs: list[tuple[str, str]] = []
    for sport in sports.get("sports", []) or []:
        sport_name = str(sport.get("name", ""))
        if not sport_name:
            continue
        try:
            detail = await manager.call_tool("tab_sport", {"sport": sport_name})
        except Exception as e:
            logger.warning("tab sport %s failed: %s", sport_name, e)
            continue
        for comp in detail.get("competitions", []) or []:
            if comp.get("name"):
                pairs.append((sport_name, str(comp["name"])))
    window = _take_rotating("tab_all", pairs, comps_per_cycle)
    out: list[dict[str, Any]] = []
    for sport_name, comp_name in window:
        try:
            payload = await manager.call_tool(
                "tab_competition",
                {"sport": sport_name, "competition": comp_name, "numTopMarkets": 1},
            )
            out.append({"sport": _slug(sport_name), "payload": payload})
        except Exception as e:
            logger.warning("tab %s/%s failed: %s", sport_name, comp_name, e)
    return {"competitions": out}


async def fetch_betr_all(manager: Any) -> dict[str, Any]:
    """BetR: event types → one master-category call per sport (prices ride inline)."""
    types = await manager.call_tool("betr_event_types", {})
    out: list[dict[str, Any]] = []
    for row in types.get("Items", []) or []:
        if not row.get("IsEventExist"):
            continue
        try:
            payload = await manager.call_tool(
                "betr_master_category", {"EventTypeId": int(row["EventTypeId"])}
            )
            out.append({"sport": _slug(str(row.get("EventTypeDesc", "?"))), "payload": payload})
        except Exception as e:
            logger.warning("betr type %s failed: %s", row.get("EventTypeDesc"), e)
    return {"types": out}


async def fetch_pointsbet_all(manager: Any) -> dict[str, Any]:
    """PointsBet hot tier: competition LISTINGS only — their inline insight/featured
    markets capture generically, and the ~5MB per-event details belong solely to
    pointsbet_books (they were being fetched by both feeds; B6)."""
    import datetime as _dt

    listing = await manager.call_tool(
        "pointsbet_sports_list", {"date": _dt.date.today().isoformat()}
    )
    comp_keys: list[int] = []
    for sport in listing.get("sports", []) or []:
        if sport.get("disabled"):
            continue
        for comp in sport.get("competitions", []) or []:  # nested directly, keys are strings
            if comp.get("key") is not None:
                comp_keys.append(int(comp["key"]))
    events: list[Any] = []
    for key in _take_rotating("pointsbet_all", comp_keys, 10):
        try:
            page = await manager.call_tool("pointsbet_competition_events", {"competitionKey": key})
            events.extend(e for e in page.get("events", []) or [] if e.get("key") is not None)
        except Exception as e:
            logger.warning("pointsbet competition %s failed: %s", key, e)
    return {"events": events}


# Fallback only — used when application-context discovery returns nothing.
_FANDUEL_FALLBACK_PAGES = ["nba", "mlb", "nhl", "wnba", "mls", "ufc"]


async def discover_fanduel_pages(manager: Any) -> list[str]:
    """FanDuel's nav scaffolding links every promoted sport page as
    ``sportsbook.fanduel.com/navigation/{slug}`` — those slugs ARE the
    content-page ids, so the page list tracks whatever FanDuel currently
    carries instead of a hand-curated list (B8)."""
    import json as _json
    import re as _re

    context = await manager.call_tool("fanduel_sb_call", {
        "operation": "application_context",
        "query_params": {"dataEntries": "AZ_BETTING,POPULAR_BETTING,QUICK_LINKS"},
    })
    slugs: list[str] = []
    for slug in _re.findall(r"/navigation/([a-z0-9-]+)", _json.dumps(context)):
        if slug not in slugs:
            slugs.append(slug)
    return slugs


async def fetch_fanduel_pages(manager: Any, *, page_ids: list[str] | None = None) -> dict[str, Any]:
    """FanDuel US: discovered sport content pages → their events' detail pages."""
    if page_ids is None:
        try:
            page_ids = await discover_fanduel_pages(manager)
        except Exception as e:
            logger.warning("fanduel page discovery failed: %s", e)
            page_ids = []
        if not page_ids:
            page_ids = list(_FANDUEL_FALLBACK_PAGES)
    pages: list[Any] = []
    for page_id in page_ids:
        try:
            part = await fetch_fanduel_event_pages(manager, page_id=page_id)
            pages.extend(part.get("pages") or [])
        except Exception as e:
            logger.warning("fanduel page %s failed: %s", page_id, e)
    return {"pages": pages}


# ─── full-book tier (60min cadence): EVERY market of every fixture ───────
# The hot tier keeps primary markets fresh; these walk the same discovery routes
# and pull each fixture's COMPLETE market book — soonest fixtures first, capped per
# cycle so the call economics stay bounded (books fill out near start time anyway).

BOOKS_PER_CYCLE = 25


async def fetch_sportsbet_books(manager: Any) -> dict[str, Any]:
    """Sportsbet full books: nav → rotating competitions → events → ``Markets``
    firehose per event (~2.5MB / ~293 markets each)."""
    nav = await manager.call_tool("sportsbet_nav_hierarchy", {})
    comps: list[tuple[int, str, str]] = []
    _walk_sportsbet_nav(nav, None, comps)
    events: list[dict[str, Any]] = []
    for comp_id, _comp_name, sport in _take_rotating("sportsbet_books_comps", comps, 15):
        try:
            payload = await manager.call_tool("sportsbet_competition_matches", {"competitionId": comp_id})
            for group in payload if isinstance(payload, list) else []:
                for event in group.get("events", []) or []:
                    if event.get("bettingStatus") == "PRICED" and event.get("id") is not None:
                        events.append({
                            "sport": sport, "event_id": str(event["id"]),
                            "event_name": str(event.get("displayName") or ""),
                            "start": event.get("startTime"),
                        })
        except Exception as e:
            logger.warning("sportsbet books comp %s failed: %s", comp_id, e)
    events.sort(key=lambda e: e.get("start") or 0)
    out: list[dict[str, Any]] = []
    for entry in events[:BOOKS_PER_CYCLE]:
        try:
            markets = await manager.call_tool(
                "sportsbet_event_markets", {"eventId": int(entry["event_id"])}
            )
            entry["markets"] = markets if isinstance(markets, list) else markets.get("markets", [])
            out.append(entry)
        except Exception as e:
            logger.warning("sportsbet book %s failed: %s", entry["event_id"], e)
    return {"events": out}


async def fetch_tab_books(manager: Any) -> dict[str, Any]:
    """TAB full books: sports tree → rotating competitions → ``tab_match`` per
    fixture (~0.8MB / ~238 markets each)."""
    sports = await manager.call_tool("tab_sports", {})
    pairs: list[tuple[str, str]] = []
    for sport in sports.get("sports", []) or []:
        name = str(sport.get("name", ""))
        if not name:
            continue
        try:
            detail = await manager.call_tool("tab_sport", {"sport": name})
            for comp in detail.get("competitions", []) or []:
                if comp.get("name"):
                    pairs.append((name, str(comp["name"])))
        except Exception as e:
            logger.warning("tab books sport %s failed: %s", name, e)
    matches: list[tuple[str, str, str]] = []  # (sport, comp, match name)
    for sport_name, comp_name in _take_rotating("tab_books_comps", pairs, 10):
        try:
            page = await manager.call_tool(
                "tab_competition", {"sport": sport_name, "competition": comp_name, "numTopMarkets": 0}
            )
            for match in page.get("matches", []) or []:
                if match.get("name"):
                    matches.append((sport_name, comp_name, str(match["name"])))
        except Exception as e:
            logger.warning("tab books comp %s/%s failed: %s", sport_name, comp_name, e)
    out: list[dict[str, Any]] = []
    for sport_name, comp_name, match_name in matches[:BOOKS_PER_CYCLE]:
        try:
            book = await manager.call_tool(
                "tab_match", {"sport": sport_name, "competition": comp_name, "match": match_name}
            )
            out.append({"sport": _slug(sport_name), "payload": book})
        except Exception as e:
            logger.warning("tab book %s failed: %s", match_name, e)
    return {"matches": out}


async def fetch_unibet_books(manager: Any) -> dict[str, Any]:
    """Kambi full books: every sport's listView (cheap) → soonest events →
    ``betoffer/event/{id}`` per fixture (~0.6MB / ~512 offers each)."""
    root = await manager.call_tool("unibet_kambi_call", {"operation": "group"})
    group = root.get("group") or root
    candidates: list[tuple[str, str, dict[str, Any]]] = []  # (start, sport, event)
    for g in group.get("groups", []) or []:
        term = g.get("termKey")
        if not term or not (g.get("boCount") or 0):
            continue
        try:
            page = await manager.call_tool(
                "unibet_kambi_call", {"operation": "sport_matches", "path_params": {"sport": str(term)}}
            )
            for item in page.get("events", []) or []:
                event = item.get("event") or {}
                if event.get("id") is not None:
                    candidates.append((str(event.get("start", "")), str(term), event))
        except Exception as e:
            logger.warning("unibet books sport %s failed: %s", term, e)
    candidates.sort(key=lambda c: c[0])
    out: list[dict[str, Any]] = []
    for _start, sport, event in candidates[:BOOKS_PER_CYCLE]:
        try:
            book = await manager.call_tool(
                "unibet_kambi_call",
                {"operation": "event_betoffer", "path_params": {"eventId": int(event["id"])}},
            )
            out.append({"sport": sport, "event": event, "betOffers": book.get("betOffers") or []})
        except Exception as e:
            logger.warning("unibet book %s failed: %s", event.get("id"), e)
    return {"events": out}


async def fetch_pinnacle_books(manager: Any) -> dict[str, Any]:
    """Pinnacle full board: rotation over ALL active matchups (not just the soonest —
    the hot tier covers those) so every fixture's straight markets refresh hourly."""
    sports = await manager.call_tool("pinnacle_sports", {})
    matchups: list[dict[str, Any]] = []
    for sport in sports or []:
        if not sport.get("matchupCount"):
            continue
        try:
            rows = await manager.call_tool("pinnacle_sport_matchups_all", {"sportId": sport["id"]})
        except Exception as e:
            logger.warning("pinnacle books sport %s failed: %s", sport.get("name"), e)
            continue
        for matchup in rows or []:
            if isinstance(matchup, dict) and matchup.get("hasMarkets"):
                matchup["_sport"] = _slug(sport.get("name", "?"))
                matchups.append(matchup)
    chosen = _take_rotating("pinnacle_books", matchups, 120)
    markets: dict[str, Any] = {}
    for matchup in chosen:
        try:
            markets[str(matchup["id"])] = await manager.call_tool(
                "pinnacle_matchup_markets", {"matchupId": matchup["id"]}
            )
        except Exception as e:
            logger.warning("pinnacle book %s failed: %s", matchup.get("id"), e)
    return {"matchups": chosen, "markets": markets}


async def fetch_pointsbet_books(manager: Any) -> dict[str, Any]:
    """PointsBet full board: rotation over ALL competitions' events → ~5MB details.
    Bandwidth-bound by design; the rotation covers the board across cycles."""
    import datetime as _dt

    listing = await manager.call_tool("pointsbet_sports_list", {"date": _dt.date.today().isoformat()})
    comp_keys: list[int] = []
    for sport in listing.get("sports", []) or []:
        if sport.get("disabled"):
            continue
        for comp in sport.get("competitions", []) or []:
            if comp.get("key") is not None:
                comp_keys.append(int(comp["key"]))
    event_keys: list[Any] = []
    for key in _take_rotating("pointsbet_books_comps", comp_keys, 12):
        try:
            page = await manager.call_tool("pointsbet_competition_events", {"competitionKey": key})
            event_keys.extend(e["key"] for e in page.get("events", []) or [] if e.get("key") is not None)
        except Exception as e:
            logger.warning("pointsbet books comp %s failed: %s", key, e)
    # soonest-first starves futures (their start dates sit months out) — reserve
    # slots for the FURTHEST-out events so outright books refresh too (B12)
    chosen = event_keys[:16] + event_keys[-4:] if len(event_keys) > 20 else event_keys
    details: list[Any] = []
    seen_keys: set[Any] = set()
    for event_key in chosen:
        if event_key in seen_keys:
            continue
        seen_keys.add(event_key)
        try:
            details.append(await manager.call_tool("pointsbet_event", {"eventKey": event_key}))
        except Exception as e:
            logger.warning("pointsbet book %s failed: %s", event_key, e)
    return {"events": details}


# ─── racing fetchers (shapes captured live 2026-06-11) ────────────────────
# Conventions proven by fanduel_racing_win: race = event, runner number =
# selection, win/place = markets. Soonest races first — racing prices move
# hardest near post.

RACES_PER_CYCLE = 15
_RACE_TYPE_SPORT = {
    "r": "horse_racing", "t": "horse_racing", "thoroughbred": "horse_racing",
    "horse": "horse_racing", "horse racing": "horse_racing",
    "g": "greyhound_racing", "greyhound": "greyhound_racing", "greyhounds": "greyhound_racing",
    "h": "harness_racing", "harness": "harness_racing", "trots": "harness_racing",
}


def _race_sport(value: Any) -> str:
    return _RACE_TYPE_SPORT.get(str(value or "").strip().lower(), _slug(str(value or "racing")))


async def fetch_tab_races(manager: Any) -> dict[str, Any]:
    """TAB: next-to-go → full racecards (runners with fixed win+place odds)."""
    ntg = await manager.call_tool("tab_racing_next_to_go", {})
    out: list[dict[str, Any]] = []
    for summary in (ntg.get("races") or [])[:RACES_PER_CYCLE]:
        meeting = summary.get("meeting") or {}
        try:
            card = await manager.call_tool("tab_racing_race", {
                "date": meeting.get("meetingDate"),
                "raceType": meeting.get("raceType"),
                "venueMnemonic": meeting.get("venueMnemonic"),
                "raceNumber": summary.get("raceNumber"),
            })
            out.append({"summary": summary, "card": card})
        except Exception as e:
            logger.warning("tab race %s/%s failed: %s", meeting.get("venueMnemonic"),
                           summary.get("raceNumber"), e)
    return {"races": out}


async def fetch_sportsbet_races(manager: Any) -> dict[str, Any]:
    """Sportsbet: AllRacing(today) → soonest future races → one BATCHED
    MultipleRacecards call (full cards incl. markets)."""
    import datetime as _dt

    allr = await manager.call_tool(
        "sportsbet_racing_allracing", {"eventDate": _dt.date.today().isoformat()}
    )
    now = _dt.datetime.now(_dt.UTC).timestamp()
    pending: list[tuple[int, float, str, dict[str, Any]]] = []
    for date_node in allr.get("dates", []) or []:
        for section in date_node.get("sections", []) or []:
            sport = _race_sport(section.get("raceType"))
            for meeting in section.get("meetings", []) or []:
                intl = 1 if meeting.get("isInternational") else 0
                for event in meeting.get("events", []) or []:
                    start = float(event.get("startTime") or 0)
                    if start > now and event.get("id") is not None:
                        event["_meeting"] = meeting.get("name")
                        pending.append((intl, start, sport, event))
    # domestic meetings first — internationals run SP-only until near post
    pending.sort(key=lambda p: (p[0], p[1]))
    chosen = [(start, sport, event) for _intl, start, sport, event in pending[:RACES_PER_CYCLE]]
    if not chosen:
        return {"events": [], "sports": {}}
    cards = await manager.call_tool(
        "sportsbet_multiple_racecards", {"eventIds": [e["id"] for _s, _sp, e in chosen]}
    )
    sports = {str(e["id"]): sport for _s, sport, e in chosen}
    meetings = {str(e["id"]): str(e.get("_meeting") or "") for _s, _sp, e in chosen}
    return {"events": cards.get("events") or [], "sports": sports, "meetings": meetings}


async def fetch_betr_races(manager: Any) -> dict[str, Any]:
    """BetR: Next5Races (all codes) → full racecards (Outcomes with WIN+PLC prices)."""
    n5 = await manager.call_tool("betr_next5_races", {"EventTypeFilter": 7, "CountryFilter": 0})
    out: list[dict[str, Any]] = []
    for item in (n5.get("Items") or [])[:RACES_PER_CYCLE]:
        race = item.get("Race") or {}
        if race.get("EventId") is None:
            continue
        try:
            card = await manager.call_tool("betr_race", {"eventId": race["EventId"]})
            out.append({"sport": _race_sport(item.get("EventType")), "card": card})
        except Exception as e:
            logger.warning("betr race %s failed: %s", race.get("EventId"), e)
    return {"races": out}


async def fetch_pointsbet_races(manager: Any) -> dict[str, Any]:
    """PointsBet: meetings window → soonest races → full racecards (runner
    fluctuations carry the current fixed win price)."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    meets = await manager.call_tool("pointsbet_racing_meetings", {
        "startDate": now.strftime("%Y-%m-%dT00:00:00.000Z"),
        "endDate": (now + _dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z"),
    })
    pending: list[tuple[str, Any]] = []
    for group in meets if isinstance(meets, list) else []:
        for meeting in group.get("meetings", []) or []:
            for race in meeting.get("races", []) or []:
                start = str(race.get("advertisedStartDateTimeUtc") or "")
                if (race.get("raceId") is not None and not race.get("placing")
                        and start >= now.strftime("%Y-%m-%dT%H:%M:%S")):
                    pending.append((start, race["raceId"]))
    pending.sort(key=lambda p: p[0])
    out: list[Any] = []
    for _start, race_id in pending[:RACES_PER_CYCLE]:
        try:
            out.append(await manager.call_tool("pointsbet_racing_race", {"raceId": race_id}))
        except Exception as e:
            logger.warning("pointsbet race %s failed: %s", race_id, e)
    return {"races": out}


async def fetch_unibet_races(manager: Any) -> dict[str, Any]:
    """Unibet: MeetingsByDateRange (T/G/H, AUS) → EventQuery per race (competitor
    prices: FixedWin/FixedPlace flucs, productType Current)."""
    import datetime as _dt

    today = _dt.date.today().isoformat()
    mbd = await manager.call_tool("unibet_racing_call", {
        "operation": "MeetingsByDateRange",
        "variables": {"startDateTime": f"{today}T00:00:00Z", "endDateTime": f"{today}T23:59:59Z",
                      "countryCodes": ["AUS"], "raceTypes": ["T", "G", "H"],
                      "clientCountryCode": "AU"},
    })
    meets = (((mbd.get("data") or {}).get("viewer") or {}).get("meetingsByDateRange")) or []
    # eventKey stamps are MEETING-day codes (all of a meeting's races share one), so
    # race times aren't knowable from the listing — fetch candidates (skipping
    # resulted ones) and keep cards that actually carry fixed prices.
    keys: list[tuple[str, str]] = []
    for meeting in meets:
        race_type = str(meeting.get("raceType") or "")
        for event in meeting.get("events", []) or []:
            key = str(event.get("eventKey") or "")
            name = str(event.get("name") or "")
            status = str(event.get("resultStatus") or "")
            if key and not name.endswith("Races Today") and status in ("", "Unknown", "None"):
                keys.append((race_type, key))
    out: list[dict[str, Any]] = []
    for race_type, key in keys[: RACES_PER_CYCLE * 3]:
        if len(out) >= RACES_PER_CYCLE:
            break
        try:
            card = await manager.call_tool("unibet_racing_call", {
                "operation": "EventQuery",
                "variables": {"eventKey": key, "clientCountryCode": "AU"},
            })
            data = card.get("data") or {}
            event = ((data.get("viewer") or {}).get("event")) or data.get("event") or {}
            if not event.get("hasFixedPrices"):
                continue  # not priced yet (or already run) — try the next race
            out.append({"sport": _race_sport(race_type), "eventKey": key, "card": card})
        except Exception as e:
            logger.warning("unibet race %s failed: %s", key, e)
    return {"races": out}


# ─── racing FUTURES tier (B11; shapes captured live 2026-06-11) ────────────
# Ante-post markets: Cup outrights, carnival winners — priced months out, slow
# moving, so the tier rotates a window per cycle at full-book cadence.

FUTURES_PER_CYCLE = 15


async def fetch_tab_racing_futures(manager: Any) -> dict[str, Any]:
    """TAB: futures meetings → one futures racecard per listed market. The card
    route puts the race NAME in the race slot (raceNumber is always 0 for
    futures), hence the dedicated MCP op."""
    listing = await manager.call_tool("tab_racing_futures_meetings", {})
    wanted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for meeting in listing.get("meetings", []) or []:
        for race in meeting.get("races", []) or []:
            if race.get("raceName") and race.get("meetingDate"):
                wanted.append((meeting, race))
    out: list[dict[str, Any]] = []
    for meeting, race in _take_rotating("tab_racing_futures", wanted, FUTURES_PER_CYCLE):
        try:
            card = await manager.call_tool("tab_racing_futures_race", {
                "date": race["meetingDate"],
                "raceType": meeting.get("raceType"),
                "venueMnemonic": meeting.get("meetingName"),
                "raceName": race["raceName"],
            })
            summary = {
                "meeting": {
                    "meetingDate": race.get("meetingDate"),
                    "raceType": meeting.get("raceType"),
                    "venueMnemonic": meeting.get("meetingName"),
                    "meetingName": meeting.get("meetingName"),
                },
                "raceNumber": 0,
                "raceName": race.get("raceName"),
                "raceStartTime": race.get("raceStartTime"),
            }
            out.append({"summary": summary, "card": card})
        except Exception as e:
            logger.warning("tab futures %s failed: %s", race.get("raceName"), e)
    return {"races": out}


async def fetch_sportsbet_racing_futures(manager: Any) -> dict[str, Any]:
    """Sportsbet: the Futures listing is sportsbook-shaped events (classId 5) — the
    standard ``event_markets`` route prices them (win+place per selection)."""
    listing = await manager.call_tool("sportsbet_racing_futures", {})
    priced = [
        e for e in (listing if isinstance(listing, list) else [])
        if e.get("bettingStatus") == "PRICED" and e.get("id") is not None
    ]
    out: list[dict[str, Any]] = []
    for event in _take_rotating("sportsbet_racing_futures", priced, FUTURES_PER_CYCLE):
        try:
            markets = await manager.call_tool("sportsbet_event_markets", {"eventId": int(event["id"])})
            out.append({
                # className e.g. "Horse Racing: Futures - AUS/NZ" — the code before
                # the colon names the sport
                "sport": _race_sport(str(event.get("className", "")).split(":")[0]),
                "event_id": str(event["id"]),
                "event_name": str(event.get("name") or event.get("competitionName") or ""),
                "start": event.get("startTime"),
                "markets": markets if isinstance(markets, list) else markets.get("markets", []),
            })
        except Exception as e:
            logger.warning("sportsbet racing future %s failed: %s", event.get("id"), e)
    return {"events": out}


async def fetch_pointsbet_racing_futures(manager: Any) -> dict[str, Any]:
    """PointsBet: racing-futures listing → standard event details (the same
    fixedOddsMarkets shape the sports normalizer reads)."""
    listing = await manager.call_tool("pointsbet_racing_futures", {})
    events = [e for e in (listing.get("events") or []) if e.get("key") is not None]
    out: list[Any] = []
    for event in _take_rotating("pointsbet_racing_futures", events, FUTURES_PER_CYCLE):
        try:
            out.append(await manager.call_tool("pointsbet_event", {"eventKey": event["key"]}))
        except Exception as e:
            logger.warning("pointsbet racing future %s failed: %s", event.get("key"), e)
    return {"events": out}


async def fetch_unibet_racing_futures(manager: Any) -> dict[str, Any]:
    """Unibet: FuturesQuery lists ante-post eventKeys — the standard EventQuery
    cards price them (competitor prices carry a direct price; flucs are empty
    for ante-post)."""
    listing = await manager.call_tool("unibet_racing_call", {"operation": "FuturesQuery", "variables": {}})
    futures = (((listing.get("data") or {}).get("viewer") or {}).get("futures")) or []
    wanted = [f for f in futures if f.get("eventKey") and f.get("hasFixedPrices")]
    out: list[dict[str, Any]] = []
    for future in _take_rotating("unibet_racing_futures", wanted, FUTURES_PER_CYCLE):
        key = str(future["eventKey"])
        try:
            card = await manager.call_tool("unibet_racing_call", {
                "operation": "EventQuery",
                "variables": {"eventKey": key, "clientCountryCode": "AU"},
            })
            out.append({"sport": _race_sport(future.get("raceType")), "eventKey": key, "card": card})
        except Exception as e:
            logger.warning("unibet racing future %s failed: %s", key, e)
    return {"races": out}


# ─── prediction markets (Kalshi / Polymarket) ──────────────────────────────

KALSHI_PAGES_PER_CYCLE = 10  # cursor pages of open events (nested markets ride along)
KALSHI_SPORTS_SERIES_PER_CYCLE = 25  # long-tail Sports series rotation (~14h sweep)
KALSHI_GAME_SERIES_PER_CYCLE = 60  # *GAME product lines revisit fast (~45min) — game lines move
POLYMARKET_PAGES_PER_CYCLE = 10  # offset pages, volume-ordered (the liquid board first)


async def fetch_kalshi_all(manager: Any) -> dict[str, Any]:
    """Kalshi: open events WITH nested markets — one paginated walk delivers titles,
    categories and live yes/no quotes together. Cursors are opaque (no stateless
    rotation), so each cycle re-reads from the top; the page cap bounds the cycle.
    Game contracts live DEEP in the board (the broad walk surfaces mostly
    elections), so a rotating window over the Sports SERIES catalogue pulls each
    league's events directly — that's where exchange-vs-book pricing comes from."""
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(KALSHI_PAGES_PER_CYCLE):
        args: dict[str, Any] = {"limit": 100, "status": "open", "with_nested_markets": True}
        if cursor:
            args["cursor"] = cursor
        try:
            payload = await manager.call_tool("kalshi_events", args)
        except Exception as e:
            logger.warning("kalshi events page failed: %s", e)
            break
        pages.append(payload)
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    try:
        series = await manager.call_tool("kalshi_series_list", {"category": "Sports"})
        tickers = [str(s["ticker"]) for s in series.get("series", []) or [] if s.get("ticker")]
    except Exception as e:
        logger.warning("kalshi sports series list failed: %s", e)
        tickers = []
    # two tiers by Kalshi's own product naming: *GAME series are the per-game
    # winner lines (the exchange-vs-book signal) and rotate fast; everything
    # else (futures, awards, novelty) rides the long tail. The `frequency`
    # field is no help — GAME series are almost all "custom" (probed live).
    game = [t for t in tickers if t.endswith("GAME")]
    tail = [t for t in tickers if not t.endswith("GAME")]
    window = (_take_rotating("kalshi_game_series", game, KALSHI_GAME_SERIES_PER_CYCLE)
              + _take_rotating("kalshi_sports_series", tail, KALSHI_SPORTS_SERIES_PER_CYCLE))
    for ticker in window:
        try:
            payload = await manager.call_tool("kalshi_events", {
                "limit": 200, "status": "open", "with_nested_markets": True,
                "series_ticker": ticker,
            })
        except Exception as e:
            logger.warning("kalshi series %s failed: %s", ticker, e)
            continue
        if isinstance(payload, dict) and payload.get("events"):
            pages.append(payload)
    return {"pages": pages}


async def fetch_polymarket_all(manager: Any) -> dict[str, Any]:
    """Polymarket: active Gamma events (markets ride nested), volume-ordered so the
    liquid board lands first; offset pages, capped per cycle. NOTE: the Gamma edge
    geo-blocks some regions — failures surface in feed_health, the feed is ready
    wherever the edge answers."""
    pages: list[Any] = []
    for page in range(POLYMARKET_PAGES_PER_CYCLE):
        try:
            payload = await manager.call_tool("polymarket_events", {
                "limit": 100, "offset": page * 100,
                "active": True, "closed": False, "order": "volume24hr",
            })
        except Exception as e:
            logger.warning("polymarket events page %s failed: %s", page, e)
            break
        rows = payload if isinstance(payload, list) else []
        if not rows:
            break
        pages.append(rows)
        if len(rows) < 100:
            break
    return {"pages": pages}


DABBLE_COMPETITIONS_PER_CYCLE = 8


async def fetch_dabble_all(manager: Any) -> dict[str, Any]:
    """Dabble: active competitions → per-competition fixture listings (featured
    prices inline) → per-fixture details for the FULL board (300+ markets on a
    liquid fixture, incl. quarter/half derivatives and the playerProps block
    that joins Pick'em stat lines onto priced selections). RACING rides the
    SAME routes (verified live 2026-07-06): each race is a fixture whose
    details carry Fixed/SP win-place + exotics as Racing* resultingTypes —
    meeting competitions rotate through with everything else."""
    listing = await manager.call_tool("dabble_active_competitions", {})
    comps = ((listing or {}).get("data") or {}).get("activeCompetitions") or []
    comps = [c for c in comps if c.get("id")]
    out: list[dict[str, Any]] = []
    details_budget = MAX_EVENTS_PER_CYCLE
    for comp in _take_rotating("dabble_all", comps, DABBLE_COMPETITIONS_PER_CYCLE):
        try:
            fixtures = await manager.call_tool(
                "dabble_competition_fixtures", {"competitionId": str(comp["id"])}
            )
        except Exception as e:  # one competition failing must not sink the cycle
            logger.warning("dabble competition %s failed: %s", comp.get("name"), e)
            continue
        rows = [f for f in ((fixtures or {}).get("data") or []) if isinstance(f, dict)]
        details: list[Any] = []
        for fixture in rows:
            if details_budget <= 0:
                break
            fixture_id = fixture.get("id")
            if not fixture_id or str(fixture.get("status", "Open")) not in ("Open", ""):
                continue
            try:
                detail = await manager.call_tool(
                    "dabble_fixture_details", {"fixtureId": str(fixture_id)}
                )
                body = (detail or {}).get("sportFixtureDetail") or detail
                if isinstance(body, dict):
                    details.append(body)
                    details_budget -= 1
            except Exception as e:
                logger.warning("dabble fixture %s detail failed: %s", fixture_id, e)
        out.append({
            "sport": str(comp.get("sportName", "?")),
            "competition": str(comp.get("name", "?")),
            "fixtures": rows,
            "details": details,
        })
    return {"competitions": out}


# Betfair navigation event types we capture (the exchange's own tree ids).
BETFAIR_EVENT_TYPES: dict[str, str] = {
    "7": "horse_racing", "4339": "greyhound_racing", "61420": "australian_rules",
    "1477": "rugby_league", "2": "tennis", "1": "soccer", "4": "cricket",
    "7522": "basketball", "6423": "american_football", "7511": "baseball", "3": "golf",
}
BETFAIR_TYPES_PER_CYCLE = 4
# racing is pinned into every cycle, so the cap leaves room for the rotating
# sports window too; override with SPORTSDATA_AGENTS_BETFAIR_MARKETS_PER_CYCLE
BETFAIR_MARKETS_PER_CYCLE = 600
_BETFAIR_PRICE_BATCH = 25


async def fetch_betfair_all(manager: Any) -> dict[str, Any]:
    """Betfair exchange, NO API key: navigation with MARKET attachments hands
    back market ids directly (1000+ per event type in one call — probed live
    2026-07-06), then bymarket returns the full back/lay ladders. The route
    split is load-bearing: byevent STRIPS exchange prices and 400s on
    multi-id batches, so it is not used at all. Uses the public `_ak` web
    key the site itself uses."""
    # racing rides EVERY cycle — it is the racing_value scan's fair-price
    # source and races jump all day; on the shared rotation it only came up
    # every ~110 minutes, so the exchange fair was stale for most races.
    # The remaining sports share the rotating window as before.
    racing = [t for t in ("7", "4339") if t in BETFAIR_EVENT_TYPES]
    others = sorted(set(BETFAIR_EVENT_TYPES) - set(racing))
    type_ids = racing + _take_rotating("betfair_all", others, BETFAIR_TYPES_PER_CYCLE)
    # (market id, start time) — SOONEST-STARTING FIRST under the cycle cap, so
    # what is ON RIGHT NOW (tonight's dogs/harness, the next UK race) always
    # beats tomorrow's cards. The old first-N slice let one busy type's future
    # card crowd every other type out entirely (lived: 443 horse markets ate a
    # 300 cap and tonight's greyhounds captured ZERO while races were running).
    found: list[tuple[str, str]] = []
    for type_id in type_ids:
        try:
            nav = await manager.call_tool("betfair_navigation", {
                # string_csv params cross the MCP layer as LISTS (the
                # dispatcher joins them) — a joined string fails validation
                "nodeIds": [f"EVENT_TYPE:{type_id}"],
                "attachments": ["MENU", "EVENT", "MARKET"],
                "maxOutDistance": 5, "maxResults": 150,
            })
        except Exception as e:
            logger.warning("betfair navigation %s failed: %s", type_id, e)
            continue
        for n in nav.get("nodes", []) or []:
            if n.get("nodeType") != "MARKET" or ":" not in str(n.get("nodeId", "")):
                continue
            info = n.get("marketInfo") or {}
            found.append((str(n["nodeId"]).split(":", 1)[1],
                          str(info.get("marketTime") or "9999")))
    found.sort(key=lambda pair: pair[1])
    cap = int(os.environ.get("SPORTSDATA_AGENTS_BETFAIR_MARKETS_PER_CYCLE",
                             str(BETFAIR_MARKETS_PER_CYCLE)))
    market_ids = [mid for mid, _ in found[:cap]]
    batches: list[Any] = []
    for start in range(0, len(market_ids), _BETFAIR_PRICE_BATCH):
        batch = market_ids[start:start + _BETFAIR_PRICE_BATCH]
        try:
            batches.append(await manager.call_tool("betfair_market_prices",
                                                   {"marketIds": batch}))
        except Exception as e:
            logger.warning("betfair bymarket batch failed: %s", e)
    return {"batches": batches}
