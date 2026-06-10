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
