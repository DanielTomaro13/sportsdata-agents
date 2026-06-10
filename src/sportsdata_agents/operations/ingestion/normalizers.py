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


def normalize_nba_odds(payload: dict[str, Any]) -> list[PricePoint]:
    """The NBA CDN odds feed: {games:[{gameId, markets:[{name, books:[{name, outcomes}]}]}]}.

    - odds arrive as strings → float; unparseable outcomes are skipped, not fatal;
    - the same book repeats per country (identical prices) → first sighting wins;
    - spread/total lines are part of the selection identity ("home -1.5"), because a
      price at -1.5 and a price at -2.5 are different markets to a backtest.
    """
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
