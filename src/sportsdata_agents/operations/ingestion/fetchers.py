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


# ─── discovery fetchers: capture EVERYTHING a provider currently prices ──
# Each walks the book's own discovery route, so coverage tracks the book, not a
# hand-curated id list. Sport labels ride inside the combined payload (the "_sport"
# convention) — slugs are each book's own naming; canonical cross-book sport ids
# belong to the event-resolution milestone.

ROTATE_CAP = 40  # markets-bearing calls per cycle for the N+1 providers
_rotation: dict[str, int] = {}  # feed-name → round-robin offset (process-lifetime)


def _take_rotating(name: str, items: list[Any], cap: int) -> list[Any]:
    """A rotating window over a long discovery list: every cycle advances the
    offset, so the whole board gets covered across cycles without N calls at once."""
    if len(items) <= cap:
        return items
    offset = _rotation.get(name, 0) % len(items)
    _rotation[name] = offset + cap
    window = items[offset : offset + cap]
    if len(window) < cap:
        window += items[: cap - len(window)]
    return window


def _slug(name: str) -> str:
    return "_".join(str(name).strip().lower().split())


async def fetch_unibet_all(manager: Any) -> dict[str, Any]:
    """Kambi: group.json → every sport termKey → one listView call per sport
    (matches + Head to Head/Line/Totals offers inline)."""
    root = await manager.call_tool("unibet_kambi_call", {"operation": "group"})
    group = root.get("group") or root
    sports: list[str] = []
    for g in group.get("groups", []) or []:
        term = g.get("termKey")
        if term and (g.get("boCount") or 0) > 0:
            sports.append(str(term))
    out: list[dict[str, Any]] = []
    for term in sports:
        try:
            payload = await manager.call_tool(
                "unibet_kambi_call", {"operation": "sport_matches", "path_params": {"sport": term}}
            )
            out.append({"sport": term, "payload": payload})
        except Exception as e:
            logger.warning("unibet sport %s failed: %s", term, e)
    return {"sports": out}


# The data plane's documented Entain sport-category UUIDs (documentation/Entain.md;
# NOVELTY and POLITICS excluded — not sports).
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


async def fetch_entain_all(manager: Any) -> dict[str, Any]:
    """Entain: one event-request per sport category (events+markets+prices in bulk)."""
    out: list[dict[str, Any]] = []
    for sport, category_id in ENTAIN_SPORT_CATEGORIES.items():
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
    """Sportsbet: navigation hierarchy → every listed competition → matches for a
    rotating window of them (the dated classes route 400s upstream; nav is live)."""
    nav = await manager.call_tool("sportsbet_nav_hierarchy", {})
    comps: list[tuple[int, str, str]] = []
    _walk_sportsbet_nav(nav, None, comps)
    window = _take_rotating("sportsbet_all", comps, ROTATE_CAP)
    out: list[dict[str, Any]] = []
    for comp_id, _comp_name, sport in window:
        try:
            payload = await manager.call_tool(
                "sportsbet_competition_matches", {"competitionId": comp_id}
            )
            out.append({"sport": sport, "payload": payload})
        except Exception as e:
            logger.warning("sportsbet competition %s failed: %s", comp_id, e)
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


async def fetch_pointsbet_all(manager: Any, *, max_event_details: int = 12) -> dict[str, Any]:
    """PointsBet: the sports list carries every competition; event listings are
    cheap, but Match Result prices need ~5MB per-event details — so only the
    soonest events board-wide get detailed each cycle."""
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
    events: list[dict[str, Any]] = []
    for key in _take_rotating("pointsbet_all", comp_keys, 10):
        try:
            page = await manager.call_tool("pointsbet_competition_events", {"competitionKey": key})
            events.extend(e for e in page.get("events", []) or [] if e.get("key") is not None)
        except Exception as e:
            logger.warning("pointsbet competition %s failed: %s", key, e)
    events.sort(key=lambda e: str(e.get("startsAt", "")))
    details: list[Any] = []
    for event in events[:max_event_details]:
        try:
            details.append(await manager.call_tool("pointsbet_event", {"eventKey": event["key"]}))
        except Exception as e:
            logger.warning("pointsbet event %s failed: %s", event.get("key"), e)
    return {"events": details}


async def fetch_fanduel_pages(manager: Any, *, page_ids: list[str]) -> dict[str, Any]:
    """FanDuel US: several sport content pages → their events' detail pages."""
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
    details: list[Any] = []
    for event_key in event_keys[:20]:
        try:
            details.append(await manager.call_tool("pointsbet_event", {"eventKey": event_key}))
        except Exception as e:
            logger.warning("pointsbet book %s failed: %s", event_key, e)
    return {"events": details}
