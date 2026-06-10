"""Feed payload → ``PricePoint`` normalizers (M2.1).

Each upstream feed has its own shape; a normalizer is a PURE function from one raw
payload to flat price points — deterministic, fixture-testable, no I/O. Quirks are
handled here so the store stays generic (the NBA CDN feed, for example, repeats a
bookmaker per country with identical prices, and serves odds as strings).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PricePoint:
    """One (feed, book, event, market, selection) price observation."""

    provider: str
    book: str
    sport: str
    event_external_id: str
    market: str
    selection: str
    odds: float
    event_name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.provider, self.book, self.event_external_id, self.market, self.selection)


def normalize_nba_odds(payload: Any) -> list[PricePoint]:
    """The NBA CDN odds feed: {games:[{gameId, markets:[{name, books:[{name, outcomes}]}]}]}.

    - odds arrive as strings → float; unparseable outcomes are skipped, not fatal;
    - the same book repeats per country (identical prices) → first sighting wins;
    - spread/total lines are part of the selection identity ("home -1.5"), because a
      price at -1.5 and a price at -2.5 are different markets to a backtest.
    """
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for game in payload.get("games", []) or []:
        game_id = str(game.get("gameId", ""))
        if not game_id:
            continue
        for market in game.get("markets", []) or []:
            market_name = str(market.get("name", "?"))
            for book in market.get("books", []) or []:
                book_name = str(book.get("name", "?"))
                for outcome in book.get("outcomes", []) or []:
                    side = str(outcome.get("type", "?"))
                    spread = outcome.get("spread")
                    selection = f"{side} {spread}" if spread not in (None, "") else side
                    try:
                        odds = float(outcome["odds"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if odds < 1.01:
                        continue
                    point = PricePoint(
                        provider="nba_cdn",
                        book=book_name,
                        sport="nba",
                        event_external_id=game_id,
                        market=market_name,
                        selection=selection,
                        odds=odds,
                        meta={"trend": outcome.get("odds_trend"), "opening": outcome.get("opening_odds")},
                    )
                    if point.key in seen:  # per-country repeats of one book
                        continue
                    seen.add(point.key)
                    points.append(point)
    return points


def _is_list(payload: Any) -> list[Any]:
    return payload if isinstance(payload, list) else []


def normalize_sportsbet_matches(payload: Any, *, sport: str) -> list[PricePoint]:
    """Sportsbet ``sportsbet_competition_matches``: a LIST of groups, each
    {groupName, events:[{id, displayName, bettingStatus, primaryMarket:{marketSort,
    selections:[{name, resultType H|A|D, price:{winPrice}}]}}]} (shape captured live
    2026-06-11, AFL competition 4165).

    Selections normalise to home/away/draw via resultType so cross-feed keys agree.
    """
    side_by_result = {"H": "home", "A": "away", "D": "draw"}
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for group in _is_list(payload):
        if not isinstance(group, dict):
            continue
        for event in group.get("events", []) or []:
            if event.get("bettingStatus") != "PRICED":
                continue
            market = event.get("primaryMarket") or {}
            event_id = str(event.get("id", ""))
            if not event_id or not market:
                continue
            market_key = "h2h" if market.get("marketSort") == "HH" else str(market.get("name", "?")).lower()
            for sel in market.get("selections", []) or []:
                try:
                    odds = float((sel.get("price") or {}).get("winPrice") or 0)
                except (TypeError, ValueError):
                    continue
                if odds < 1.01:
                    continue
                selection = side_by_result.get(str(sel.get("resultType", "")), str(sel.get("name", "?")).lower())
                point = PricePoint(
                    provider="sportsbet",
                    book="Sportsbet",
                    sport=sport,
                    event_external_id=event_id,
                    event_name=str(event.get("displayName") or event.get("name") or ""),
                    market=market_key,
                    selection=selection,
                    odds=odds,
                    meta={"start_time": event.get("startTime"), "team": sel.get("name")},
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)
    return points


def normalize_tab_competition(payload: Any, *, sport: str) -> list[PricePoint]:
    """TAB ``tab_competition`` (numTopMarkets>=1): {matches:[{id, name, startTime,
    markets:[{betOption, propositions:[{name, returnWin, position HOME|AWAY,
    bettingStatus}]}]}]} (shape captured live 2026-06-11, "AFL Football"/"AFL").

    Only the Head To Head bet option is ingested from the inline top markets;
    propositions normalise to home/away via ``position``.
    """
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for match in payload.get("matches", []) or []:
        match_id = str(match.get("spectatorBettingId") or match.get("id") or "")
        if not match_id:
            continue
        for market in match.get("markets", []) or []:
            if str(market.get("betOption", "")) != "Head To Head":
                continue
            for prop in market.get("propositions", []) or []:
                if str(prop.get("bettingStatus", "")) not in ("Open", "Live"):
                    continue
                try:
                    odds = float(prop.get("returnWin") or 0)
                except (TypeError, ValueError):
                    continue
                if odds < 1.01:
                    continue
                position = str(prop.get("position", "")).lower()
                selection = position if position in ("home", "away", "draw") else str(prop.get("name", "?")).lower()
                point = PricePoint(
                    provider="tab",
                    book="TAB",
                    sport=sport,
                    event_external_id=match_id,
                    event_name=str(match.get("name") or ""),
                    market="h2h",
                    selection=selection,
                    odds=odds,
                    meta={"start_time": match.get("startTime"), "team": prop.get("name")},
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)
    return points


# ─── shared helpers for the multi-book feeds ──────────────────────────────


def _side_from_event_name(outcome_name: str, event_name: str) -> str | None:
    """home/away by matching the outcome against an "X v Y" / "X vs Y" event name
    (BetR uses " v ", Entain " vs ")."""
    sep = " vs " if " vs " in event_name else " v " if " v " in event_name else None
    if sep is None:
        return None
    home, away = (part.strip() for part in event_name.split(sep, 1))
    o = outcome_name.strip().lower()
    if o and (o == home.lower() or o in home.lower() or home.lower() in o):
        return "home"
    if o and (o == away.lower() or o in away.lower() or away.lower() in o):
        return "away"
    return None


def american_to_decimal(price: float) -> float:
    """+194 → 2.94; -257 → 1.389 (Pinnacle quotes American odds)."""
    if price >= 100:
        return 1.0 + price / 100.0
    if price <= -100:
        return 1.0 + 100.0 / abs(price)
    raise ValueError(f"not an American price: {price}")


def normalize_unibet_matches(payload: Any, *, sport: str) -> list[PricePoint]:
    """Unibet/Kambi listView matches: {events:[{event:{id, homeName, awayName, start},
    betOffers:[{betOfferType:{name}, criterion:{lifetime}, outcomes:[{type OT_ONE|OT_TWO|
    OT_CROSS, odds <milli>, participant}]}]}]} (captured live 2026-06-11).

    Kambi odds are integers x1000 (1730 -> 1.73); the Head to Head offer over
    full time is the h2h market.
    """
    side_by_type = {"OT_ONE": "home", "OT_TWO": "away", "OT_CROSS": "draw"}
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    if not isinstance(payload, dict):
        return []
    for item in payload.get("events", []) or []:
        event = item.get("event") or {}
        event_id = str(event.get("id", ""))
        if not event_id:
            continue
        for offer in item.get("betOffers", []) or []:
            if (offer.get("betOfferType") or {}).get("name") != "Head to Head":
                continue
            # AFL quotes regular time; NRL quotes including overtime — both ARE the h2h
            if (offer.get("criterion") or {}).get("lifetime") not in (None, "FULL_TIME", "FULL_TIME_OVERTIME"):
                continue
            for outcome in offer.get("outcomes", []) or []:
                if outcome.get("status") not in (None, "OPEN"):
                    continue
                raw = outcome.get("odds")
                if not isinstance(raw, int | float) or raw < 1010:
                    continue
                selection = side_by_type.get(str(outcome.get("type", "")))
                if selection is None:
                    continue
                point = PricePoint(
                    provider="unibet",
                    book="Unibet",
                    sport=sport,
                    event_external_id=event_id,
                    event_name=str(event.get("name") or ""),
                    market="h2h",
                    selection=selection,
                    odds=round(raw / 1000.0, 3),
                    meta={"start_time": event.get("start"), "team": outcome.get("participant")},
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)
    return points


def normalize_betr_category(payload: Any, *, sport: str) -> list[PricePoint]:
    """BetR sports category: MasterCategories→Categories→MasterEvents→Markets, where a
    market row is ONE outcome ({EventName "Match Result (…)", OutcomeName, Price,
    MarketDesc "Win"}) (captured live 2026-06-11). home/away resolves against the
    MasterEventName ("X v Y")."""
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    if not isinstance(payload, dict):
        return []
    for master_cat in payload.get("MasterCategories", []) or []:
        for category in master_cat.get("Categories", []) or []:
            for event in category.get("MasterEvents", []) or []:
                event_id = str(event.get("MasterEventId", ""))
                event_name = str(event.get("MasterEventName") or "")
                if not event_id:
                    continue
                for row in event.get("Markets", []) or []:
                    if str(row.get("MarketDesc", "")) != "Win":
                        continue
                    if not str(row.get("EventName", "")).startswith("Match Result"):
                        continue
                    try:
                        odds = float(row.get("Price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if odds < 1.01:
                        continue
                    selection = _side_from_event_name(str(row.get("OutcomeName", "")), event_name)
                    if selection is None:
                        continue
                    point = PricePoint(
                        provider="betr",
                        book="BetR",
                        sport=sport,
                        event_external_id=event_id,
                        event_name=event_name,
                        market="h2h",
                        selection=selection,
                        odds=odds,
                        meta={"start_time": event.get("MinAdvertisedStartTime"), "team": row.get("OutcomeName")},
                    )
                    if point.key in seen:
                        continue
                    seen.add(point.key)
                    points.append(point)
    return points


def normalize_entain_events(payload: Any, *, sport: str) -> list[PricePoint]:
    """Entain (Ladbrokes/Neds) event-request: parallel maps {events{}, markets{},
    entrants{}, prices{}} joined by UUIDs; prices keyed "<entrant_id>:<product>:"
    with fractional odds {numerator, denominator} (captured live 2026-06-11).

    Only Head To Head / Match Betting markets on open match events are ingested;
    home/away resolves against the event name ("X v Y").
    """
    if not isinstance(payload, dict):
        return []
    events = payload.get("events") or {}
    markets = payload.get("markets") or {}
    entrants = payload.get("entrants") or {}
    prices = payload.get("prices") or {}
    # entrant_id → decimal odds (first price entry per entrant wins)
    odds_by_entrant: dict[str, float] = {}
    for key, price in prices.items():
        entrant_id = str(key).split(":", 1)[0]
        if entrant_id in odds_by_entrant:
            continue
        odds_obj = (price or {}).get("odds") or {}
        num, den = odds_obj.get("numerator"), odds_obj.get("denominator")
        if isinstance(num, int | float) and isinstance(den, int | float) and den:
            odds_by_entrant[entrant_id] = 1.0 + num / den
    h2h_names = {"head to head", "match betting", "match result", "match winner"}
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for market_id, market in markets.items():
        if str(market.get("name", "")).lower() not in h2h_names:
            continue
        event = events.get(str(market.get("event_id"))) or {}
        event_name = str(event.get("name") or "")
        if (" v " not in event_name and " vs " not in event_name) or event.get("match_status") not in (
            None, "BettingOpen", "Open"
        ):
            continue
        for entrant_id, entrant in entrants.items():
            if str(entrant.get("market_id")) != str(market_id):
                continue
            odds = odds_by_entrant.get(str(entrant_id))
            if odds is None or odds < 1.01:
                continue
            selection = _side_from_event_name(str(entrant.get("name", "")), event_name)
            if selection is None:
                continue
            point = PricePoint(
                provider="entain",
                book="Ladbrokes",
                sport=sport,
                event_external_id=str(market.get("event_id")),
                event_name=event_name,
                market="h2h",
                selection=selection,
                odds=round(odds, 3),
                meta={"team": entrant.get("name")},
            )
            if point.key in seen:
                continue
            seen.add(point.key)
            points.append(point)
    return points


def normalize_pinnacle_league(payload: Any, *, sport: str) -> list[PricePoint]:
    """Pinnacle (via fetch_pinnacle_league): {matchups:[{id, participants:[{name,
    alignment}], startTime}], markets:{<id>: [{type "moneyline", key "s;0;m",
    prices:[{designation home|away|draw, price <american>}]}]}} (captured live
    2026-06-11). American odds convert to decimal; the sharpest book on the board —
    its close is the CLV benchmark."""
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    markets_by_id = payload.get("markets") or {}
    for matchup in payload.get("matchups", []) or []:
        matchup_id = str(matchup.get("id", ""))
        if not matchup_id:
            continue
        names = {
            str(p.get("alignment", "")): str(p.get("name", ""))
            for p in matchup.get("participants", []) or []
        }
        event_name = f"{names.get('home', '?')} v {names.get('away', '?')}"
        for market in markets_by_id.get(matchup_id, []) or []:
            if market.get("type") != "moneyline" or market.get("period") != 0 or market.get("isAlternate"):
                continue
            if market.get("status") not in (None, "open"):
                continue
            for price in market.get("prices", []) or []:
                designation = str(price.get("designation", ""))
                if designation not in ("home", "away", "draw"):
                    continue
                try:
                    odds = american_to_decimal(float(price.get("price")))
                except (TypeError, ValueError):
                    continue
                point = PricePoint(
                    provider="pinnacle",
                    book="Pinnacle",
                    sport=sport,
                    event_external_id=matchup_id,
                    event_name=event_name,
                    market="h2h",
                    selection=designation,
                    odds=round(odds, 3),
                    meta={"start_time": matchup.get("startTime"), "team": names.get(designation)},
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)
    return points


def normalize_pointsbet_events(payload: Any, *, sport: str) -> list[PricePoint]:
    """PointsBet (via fetch_pointsbet_competition): event details with
    fixedOddsMarkets; h2h is eventClass "Match Result" with decimal-priced outcomes;
    home/away resolves against homeTeam/awayTeam (captured live 2026-06-11)."""
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("key", ""))
        home, away = str(event.get("homeTeam") or ""), str(event.get("awayTeam") or "")
        if not event_id:
            continue
        for market in event.get("fixedOddsMarkets", []) or []:
            if str(market.get("eventClass", "")) != "Match Result":
                continue
            for outcome in market.get("outcomes", []) or []:
                try:
                    odds = float(outcome.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if odds < 1.01:
                    continue
                team = str(outcome.get("name", ""))
                if home and team.lower() == home.lower():
                    selection = "home"
                elif away and team.lower() == away.lower():
                    selection = "away"
                else:
                    selection = _side_from_event_name(team, f"{home} v {away}") or "draw"
                point = PricePoint(
                    provider="pointsbet",
                    book="PointsBet",
                    sport=sport,
                    event_external_id=event_id,
                    event_name=str(event.get("name") or f"{home} v {away}"),
                    market="h2h",
                    selection=selection,
                    odds=odds,
                    meta={"start_time": event.get("startsAt"), "team": team},
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                points.append(point)
            break  # one Match Result market per event
    return points


def normalize_betfair_by_event(payload: Any, *, sport: str) -> list[PricePoint]:
    """Betfair exchange (via fetch_betfair_event_type): eventNodes→marketNodes where
    description.marketType == MATCH_ODDS; the price is the best available BACK on
    each runner — the exchange's executable market, not a bookmaker margin price."""
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for event_type in payload.get("eventTypes", []) or []:
        for node in event_type.get("eventNodes", []) or []:
            event_id = str(node.get("eventId", ""))
            event_name = str((node.get("event") or {}).get("eventName") or "")
            for market in node.get("marketNodes", []) or []:
                if (market.get("description") or {}).get("marketType") != "MATCH_ODDS":
                    continue
                if (market.get("state") or {}).get("status") not in (None, "OPEN"):
                    continue
                runners = market.get("runners") or []
                for i, runner in enumerate(runners):
                    desc = runner.get("description") or {}
                    team = str(desc.get("runnerName") or runner.get("runnerName") or "")
                    backs = ((runner.get("exchange") or {}).get("availableToBack")) or []
                    if not backs:
                        continue
                    try:
                        odds = float(backs[0].get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if odds < 1.01:
                        continue
                    selection = _side_from_event_name(team, event_name) or (
                        "home" if i == 0 else "away" if i == 1 else "draw"
                    )
                    lay = ((runner.get("exchange") or {}).get("availableToLay") or [{}])[0].get("price")
                    point = PricePoint(
                        provider="betfair",
                        book="Betfair",
                        sport=sport,
                        event_external_id=event_id,
                        event_name=event_name,
                        market="h2h",
                        selection=selection,
                        odds=odds,
                        meta={"team": team, "lay": lay},
                    )
                    if point.key in seen:
                        continue
                    seen.add(point.key)
                    points.append(point)
    return points
