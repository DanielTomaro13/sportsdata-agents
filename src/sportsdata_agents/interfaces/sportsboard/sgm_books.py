"""Book-quoted same-game multis — the board asks the BOOK, not the engine.

The public board deliberately does not ship the engines package, so its SGM
button prices through the bookmakers themselves via sportsdata-mcp: real,
bookable prices with the book's own correlation adjustment, which no single
book's app shows side by side. The engine's correlated model stays private.

Each bookmaker is a resolver that maps the board's abstract legs
({market: h2h|total|line, selection: home|away|over|under, line}) onto the
book's own identifiers, then calls the book's SGM pricer. Every step that can
fail returns a structured refusal — {"unavailable": reason} — never a guess:
a wrong price with a bookmaker's name on it is worse than no price.

Availability is per fixture and per book (the fixture must have a linked event
for that provider, and the live poller's MCP manager must be up). TAB's
resolver is a documented gap: tab_match_markets is addressed by TAB's OWN
sport/competition/match names, which ingestion currently drops before storage
(fetch_tab_all knows them; the PricePoint keeps only a slug). Thread those
through Event.meta and the TAB resolver slots in beside the Sportsbet one.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Event, Fixture

BOOKMAKERS = ("sportsbet", "tab")  # selector order; tab reports unavailable for now


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9. ]", "", str(s).lower()).strip()


async def _linked_event(session: AsyncSession, fixture_id: str, provider: str) -> Event | None:
    try:
        fid = uuid.UUID(fixture_id)  # the column is a Uuid; a raw string blows up in the driver
    except ValueError:
        return None
    row = await session.execute(
        select(Event).where(Event.fixture_id == fid, Event.provider == provider))
    return row.scalars().first()


async def available_books(session: AsyncSession, fixture_id: str, mcp: Any) -> dict[str, Any]:
    """Which bookmakers can quote this fixture, and why the rest cannot."""
    out: dict[str, Any] = {}
    for book in BOOKMAKERS:
        if book == "tab":
            out[book] = {"available": False,
                         "reason": "TAB quoting needs its sport/competition names threaded through ingestion"}
            continue
        if mcp is None:
            out[book] = {"available": False, "reason": "live data plane not running"}
            continue
        ev = await _linked_event(session, fixture_id, book)
        out[book] = ({"available": True} if ev is not None else
                     {"available": False, "reason": f"no {book} event linked to this fixture"})
    return out


def _match_sportsbet_leg(leg: dict, markets: list[dict], home: str, away: str) -> dict | str:
    """One board leg -> {marketExternalId, outcomeExternalId}, or a reason string."""
    fam = str(leg.get("market") or "")
    sel = str(leg.get("selection") or "")
    line = leg.get("line")
    label = str(leg.get("label") or f"{fam}:{sel}")

    def open_sels(m: dict) -> list[dict]:
        return [s for s in (m.get("selections") or []) if s.get("statusCode") == "A"]

    if fam == "h2h":
        # HH is Sportsbet's two-way head-to-head tag; MR the three-way match
        # result (soccer, test cricket). Both are the fixture's h2h market.
        team = home if sel == "home" else away if sel == "away" else sel
        for m in markets:
            if m.get("marketSort") not in ("HH", "MR") or m.get("statusCode") != "A":
                continue
            want_rt = {"home": "H", "away": "A", "draw": "D"}.get(sel)
            for s in open_sels(m):
                if (want_rt and s.get("resultType") == want_rt) or _norm(s["name"]) == _norm(team):
                    return {"marketExternalId": m["externalId"], "outcomeExternalId": s["externalId"]}
        return f"{label}: no open head-to-head selection at Sportsbet"

    if fam == "total":
        # Two shapes, seen live: the line in the selection name ("Over 21.5"),
        # or a plain "Over"/"Under" with the line in unformattedHandicap (HL
        # markets). Rank match-level totals ahead of team/player sub-totals so
        # a player line that happens to equal the match line cannot shadow it.
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"

        def leg_from(m: dict) -> dict | None:
            named = _norm(f"{sel} {line}")
            for s in open_sels(m):
                sn = _norm(s["name"])
                hcp = str(s.get("unformattedHandicap") or "")
                if sn == named or (sn == sel and hcp and abs(float(hcp) - float(line)) < 1e-9):
                    return {"marketExternalId": m["externalId"], "outcomeExternalId": s["externalId"]}
            return None

        candidates = [m for m in markets
                      if m.get("statusCode") == "A" and "total" in _norm(m.get("name", ""))]
        candidates.sort(key=lambda m: 0 if _norm(m.get("name", "")).startswith("match total") else
                                      1 if not any(_norm(t) and _norm(t) in _norm(m.get("name", ""))
                                                   for t in (home, away)) else 2)
        for m in candidates:
            hit = leg_from(m)
            if hit:
                return hit
        return f"{label}: Sportsbet has no open total at line {line}"

    if fam == "line":
        # Handicap selections read "Team (+5.5)" / "Team (-5.5)".
        team = home if sel == "home" else away if sel == "away" else sel
        for m in markets:
            nm = _norm(m.get("name", ""))
            if m.get("statusCode") != "A" or not ("line" in nm or "handicap" in nm):
                continue
            for s in open_sels(m):
                sn = _norm(s["name"])
                if _norm(team) in sn and (line is None or str(abs(float(line))) in sn):
                    return {"marketExternalId": m["externalId"], "outcomeExternalId": s["externalId"]}
        return f"{label}: Sportsbet has no matching line market"

    return f"{label}: market family {fam!r} is not mapped for Sportsbet"


async def quote_sportsbet(session: AsyncSession, mcp: Any, fixture_id: str,
                          legs: list[dict]) -> dict[str, Any]:
    ev = await _linked_event(session, fixture_id, "sportsbet")
    if ev is None:
        return {"unavailable": "no Sportsbet event linked to this fixture"}
    f = (await session.execute(select(Fixture).where(Fixture.id == ev.fixture_id))).scalar()
    home, away = [*f.name.split(" v ", 1), ""][:2] if f and " v " in f.name else ("", "")

    try:
        markets = await mcp.call_tool("sportsbet_event_markets",
                                      {"eventId": int(ev.external_id)})
    except Exception as exc:
        return {"unavailable": f"sportsbet_event_markets failed: {exc}"}
    if not isinstance(markets, list) or not markets:
        return {"unavailable": "Sportsbet returned no markets for this event"}

    outcomes: list[Any] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_sportsbet_leg(leg, markets, home, away)
        (outcomes if isinstance(hit, dict) else misses).append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses}

    # The pricer wants the class/competition EXTERNAL ids; every market's
    # topicLink carries them (verified live: topicLink numbers price fine).
    tl = str(markets[0].get("topicLink") or "")
    m = re.search(r"Sports/(\d+)/Competitions/(\d+)", tl)
    if not m:
        return {"unavailable": f"could not read class/competition from topicLink {tl!r}"}

    try:
        r = await mcp.call_tool("sportsbet_sgm_price", {
            "classExternalId": int(m.group(1)),
            "competitionExternalId": int(m.group(2)),
            "eventExternalId": int(ev.external_id),
            "outcomesExternalIds": outcomes,
        })
    except Exception as exc:
        # Legs the book will not combine come back as a refusal, not a price.
        return {"unavailable": f"Sportsbet refused to price this combination: {exc}"}

    price = (r or {}).get("price") or {}
    num, den = price.get("numerator"), price.get("denominator")
    if not (isinstance(num, int) and isinstance(den, int) and den > 0):
        return {"unavailable": f"unexpected pricer response: {r!r}"}
    decimal = round(1 + num / den, 2)
    return {
        "priced_by": "sportsbet",
        "book_odds": decimal,
        "fractional": f"{num}/{den}",
        "quote_id": price.get("quoteId"),
        "legs_matched": len(outcomes),
        "warnings": ["a Sportsbet quote is short-lived — re-price before betting"],
    }


async def quote(session: AsyncSession, mcp: Any, bookmaker: str, fixture_id: str,
                legs: list[dict]) -> dict[str, Any]:
    if bookmaker not in BOOKMAKERS:
        return {"unavailable": f"unknown bookmaker {bookmaker!r} (have: {', '.join(BOOKMAKERS)})"}
    if mcp is None:
        return {"unavailable": "live data plane not running — book quotes need the in-process poller"}
    if bookmaker == "tab":
        return {"unavailable": "TAB quoting is not wired yet — see module docstring"}
    return await quote_sportsbet(session, mcp, fixture_id, legs)
