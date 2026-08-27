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
for that provider, and the live poller's MCP manager must be up).

ALL SIX BOOKS RESOLVE. Five were built against captured payloads and their leg
ids verified against the prices those books actually quoted. TAB is the
exception and says so in `_match_tab_leg`: its sports endpoints need OAuth, so
that resolver is written from the documented shapes rather than from a capture,
and a TAB miss should be read as "the resolver may be wrong" before "the book
has no market". TAB also needs no ingestion change after all — it is addressed
by NAME, and listing its own matches for the competition and matching the
fixture against them stands in for the id mapping ingestion does not keep.

`compare` is the point of the module: the same legs at every book at once, on
comparable units. See PRICE_UNITS for why the units are the load-bearing part.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Event, Fixture

BOOKMAKERS = ("sportsbet", "pointsbet", "betr", "unibet", "entain", "tab")


#: HOW EACH BOOK EXPRESSES A PRICE. Nothing in any payload announces its units, and the
#: books do not agree — so every quote goes through `_decimal_from` before it is compared
#: with another book's. This table is the single place that knowledge lives.
#:
#: Getting it wrong fails in two very different ways. Unibet un-scaled is 1000x too large
#: and screams. Entain without the +1 reads 2.70 where the truth is 3.70 — perfectly
#: plausible, and it silently loses every comparison it should have won. The second is the
#: dangerous one, which is why this is a table with tests rather than four call sites.
PRICE_UNITS = {
    "sportsbet": "fractional",     # {numerator, denominator} -> 1 + n/d
    "entain":    "fractional",     # ditto; 27/10 is 3.70, not 2.7
    "pointsbet": "decimal",        # already decimal
    "betr":      "decimal",        # already decimal
    "unibet":    "thousandths",    # 3400 -> 3.40 (Kambi scales odds AND lines by 1000)
    "tab":       "decimal",        # decimal string in odds.decimal
}


def _decimal_from(book: str, value: Any) -> float | None:
    """One book's raw price as a decimal, or None if it is not a price at all.

    Deliberately strict: a shape this does not recognise returns None so the caller
    reports "unavailable", rather than guessing a number that would carry a bookmaker's
    name on it.
    """
    unit = PRICE_UNITS.get(book)
    if unit is None:
        # A book with no declared units must not be guessed at as decimal. Falling through
        # is how a newly added book gets compared in whatever units it happens to send.
        return None
    if unit == "fractional":
        if not isinstance(value, dict):
            return None
        num, den = value.get("numerator"), value.get("denominator")
        if not (isinstance(num, (int, float)) and isinstance(den, (int, float)) and den > 0):
            return None
        return 1.0 + num / den
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "thousandths":
        f = f / 1000.0
    # A "price" of 0 is how PointsBet and BetR say NO. Treating it as a quote would put a
    # zero-odds bet at the top of a comparison sorted by price.
    return f if f > 1.0 else None


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
        if book not in _QUOTERS:
            # The mcp pricer exists; the leg resolver does not. Said plainly so the gap
            # reads as unfinished work rather than as the book refusing.
            out[book] = {"available": False,
                         "reason": f"{book}_sgm_price exists but its leg resolver is not written yet"}
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


def _match_pointsbet_leg(leg: dict, markets: list[dict], home: str, away: str) -> dict | str:
    """One board leg -> {MarketKey, OutcomeKey}, or a reason string.

    PointsBet's OutcomeKey is unique only WITHIN its market — key "11" is a different bet
    in the Line market than in the Total market — so both ids always travel together and
    an outcome key is never carried between markets.
    """
    fam, sel = str(leg.get("market") or ""), str(leg.get("selection") or "")
    line, label = leg.get("line"), str(leg.get("label") or f"{leg.get('market')}:{leg.get('selection')}")

    def outcomes(m: dict) -> list[dict]:
        return [o for o in (m.get("outcomes") or []) if o.get("isOpenForBetting")]

    def hit(m: dict, o: dict) -> dict:
        return {"MarketKey": str(m["key"]), "OutcomeKey": str(o["key"])}

    if fam == "h2h":
        team = home if sel == "home" else away if sel == "away" else sel
        for m in markets:
            if _norm(m.get("eventClass", "")) != "match result":
                continue
            for o in outcomes(m):
                if _norm(o.get("name", "")) == _norm(team):
                    return hit(m, o)
        return f"{label}: no open head-to-head selection at PointsBet"

    if fam == "total":
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"
        for m in markets:
            if "total points" not in _norm(m.get("eventClass", "")):
                continue
            for o in outcomes(m):
                on = _norm(o.get("name", ""))
                if on.startswith(sel) and str(line) in on:
                    return hit(m, o)
        return f"{label}: PointsBet has no open total at line {line}"

    if fam == "line":
        team = home if sel == "home" else away if sel == "away" else sel
        for m in markets:
            if _norm(m.get("eventClass", "")) != "line":
                continue
            for o in outcomes(m):
                on = _norm(o.get("name", ""))
                if _norm(team) in on and (line is None or str(abs(float(line))) in on):
                    return hit(m, o)
        return f"{label}: PointsBet has no matching line market"

    return f"{label}: market family {fam!r} is not mapped for PointsBet"


def _match_unibet_leg(leg: dict, offers: list[dict], home: str, away: str) -> int | str:
    """One board leg -> a Kambi outcome id, or a reason string.

    Kambi scales BOTH odds and lines by 1000, so a line of 169.5 arrives as 169500. The
    comparison here converts the board's line up rather than Kambi's down, because
    integers compare exactly and 169.5 * 1000 does not round.
    """
    fam, sel = str(leg.get("market") or ""), str(leg.get("selection") or "")
    line, label = leg.get("line"), str(leg.get("label") or f"{leg.get('market')}:{leg.get('selection')}")

    def live(o: dict) -> bool:
        return "odds" in o          # a suspended outcome carries no price at all

    def type_of(b: dict) -> str:
        return _norm((b.get("betOfferType") or {}).get("name", ""))

    if fam == "h2h":
        team = home if sel == "home" else away if sel == "away" else sel
        for b in offers:
            if type_of(b) not in ("head to head", "match"):
                continue
            for o in (b.get("outcomes") or []):
                if live(o) and _norm(o.get("participant", "")) == _norm(team):
                    return int(o["id"])
        return f"{label}: no open head-to-head selection at Unibet"

    if fam == "total":
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"
        want_line = round(float(line) * 1000)
        # Kambi ships 56 "Totals" offers on one AFL fixture and only a handful are the
        # MATCH total — the rest are per-team ("Total Points by Western Bulldogs") or per
        # period ("- Quarter 1"). A team total that happens to sit on the requested line
        # would otherwise shadow the match total and quote a different bet under the same
        # label, so match-level candidates are tried first.
        def rank(b: dict) -> int:
            crit = _norm((b.get("criterion") or {}).get("label", ""))
            if any(_norm(t) and _norm(t) in crit for t in (home, away)):
                return 2                                     # a team sub-total
            if "quarter" in crit or "half" in crit:
                return 1                                     # a period sub-total
            return 0                                         # the match total
        cands = sorted((b for b in offers if type_of(b) == "totals"), key=rank)
        for b in cands:
            for o in (b.get("outcomes") or []):
                if live(o) and _norm(o.get("label", "")) == sel and o.get("line") == want_line:
                    return int(o["id"])
        return f"{label}: Unibet has no open match total at line {line}"

    if fam == "line":
        team = home if sel == "home" else away if sel == "away" else sel
        for b in offers:
            if type_of(b) != "line":
                continue
            for o in (b.get("outcomes") or []):
                if live(o) and _norm(o.get("participant", "")) == _norm(team):
                    return int(o["id"])
        return f"{label}: Unibet has no matching line market"

    return f"{label}: market family {fam!r} is not mapped for Unibet"


#: competition INTERNAL id -> (classExternalId, competitionExternalId). Sportsbet's nav
#: hierarchy is the only place the two id spaces are related, and it is a big document, so
#: it is fetched once per process rather than per quote.
_SPORTSBET_EXT: dict[int, tuple[int, int]] = {}


async def _sportsbet_external_ids(mcp: Any, event_external_id: str,
                                  markets: list[dict]) -> tuple[int, int] | str:
    """(classExternalId, competitionExternalId) for the competition this event sits in.

    The pricer takes EXTERNAL ids. `topicLink` looks like it carries them and does not —
    it carries the internal pair, which the pricer answers with a 500.
    """
    tl = str((markets[0] if markets else {}).get("topicLink") or "")
    m = re.search(r"Competitions/(\d+)", tl)
    if not m:
        return f"could not read the competition id from topicLink {tl!r}"
    comp_internal = int(m.group(1))
    if comp_internal in _SPORTSBET_EXT:
        return _SPORTSBET_EXT[comp_internal]

    try:
        nav = await mcp.call_tool("sportsbet_nav_hierarchy", {})
    except Exception as exc:
        return f"sportsbet_nav_hierarchy failed: {exc}"

    found: list[tuple[int, int]] = []

    def walk(node: Any, class_ext: Any) -> None:
        if isinstance(node, dict):
            ce = node.get("classExternalId") or class_ext
            if node.get("id") == comp_internal and node.get("competitionExternalId") and ce:
                found.append((int(ce), int(node["competitionExternalId"])))
            for v in node.values():
                walk(v, ce)
        elif isinstance(node, list):
            for v in node:
                walk(v, class_ext)

    walk(nav, None)
    if not found:
        return f"competition {comp_internal} is not in sportsbet_nav_hierarchy"
    _SPORTSBET_EXT[comp_internal] = found[0]
    return found[0]


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

    # The pricer wants the class/competition EXTERNAL ids, and `topicLink` does NOT carry
    # them — it carries the INTERNAL ones. On AFL the topicLink reads
    # ".../Sports/50/Competitions/4165/..." while the pricer wants 103/17131, and feeding
    # it the internal pair returns HTTP 500 "Error getting price from MAP API". Verified
    # live 2026-08-27: 50/4165 fails, 103/17131 prices. Only sportsbet_nav_hierarchy
    # relates the two, so the mapping is looked up rather than parsed out of a URL.
    ids = await _sportsbet_external_ids(mcp, str(ev.external_id), markets)
    if isinstance(ids, str):
        return {"unavailable": ids}
    class_ext, comp_ext = ids

    try:
        r = await mcp.call_tool("sportsbet_sgm_price", {
            "classExternalId": class_ext,
            "competitionExternalId": comp_ext,
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


def _match_betr_leg(leg: dict, events: list[dict], home: str, away: str) -> dict | str:
    """One board leg -> {EventId, OutcomeId, MarketType}, or a reason string.

    BetR calls a MARKET GROUP an "Event" — 91686300 is Match Result and 91712212 is Total
    Points, both inside MasterEvent 2255977 — so EventId here is not the match. OutcomeId
    restarts per group (both groups have an outcome "1"), which is why the pair always
    travels together.

    MarketType is `MarketTypeCode` and it is LOAD-BEARING BUT UNVALIDATED: dropping it
    turned a verified 2.20 into 21 with ErrorNo 0, so it is copied from the outcome rather
    than inferred. `FixedWin` is deliberately never sent — BetR treats a client-supplied
    price as a floor on the answer.
    """
    fam, sel = str(leg.get("market") or ""), str(leg.get("selection") or "")
    line, label = leg.get("line"), str(leg.get("label") or f"{leg.get('market')}:{leg.get('selection')}")

    def hit(e: dict, o: dict) -> dict:
        return {"EventId": e["EventId"], "OutcomeId": o["OutcomeId"],
                "MarketType": o.get("MarketTypeCode")}

    def live(o: dict) -> bool:
        return bool(o.get("IsOpenForBetting")) and o.get("Price") is not None

    def group(e: dict, name: str) -> list[dict]:
        return [o for o in (e.get("Outcomes") or [])
                if _norm(o.get("GroupByHeader", "")) == name and live(o)]

    if fam == "h2h":
        team = home if sel == "home" else away if sel == "away" else sel
        for e in events:
            for o in group(e, "match result"):
                if _norm(o.get("OutcomeName", "")) == _norm(team):
                    return hit(e, o)
        return f"{label}: no open head-to-head selection at BetR"

    if fam == "total":
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"
        want = _norm(f"{sel} {line}")
        for e in events:
            for o in group(e, "total points"):
                if _norm(o.get("OutcomeName", "")) == want:
                    return hit(e, o)
        return f"{label}: BetR has no open total at line {line}"

    if fam == "line":
        # The handicap is on `Points`, not in the name — the name is just the team.
        team = home if sel == "home" else away if sel == "away" else sel
        for e in events:
            for o in group(e, "handicap"):
                if _norm(o.get("OutcomeName", "")) != _norm(team):
                    continue
                if line is None or abs(abs(float(o.get("Points") or 0)) - abs(float(line))) < 1e-9:
                    return hit(e, o)
        return f"{label}: BetR has no matching handicap at line {line}"

    return f"{label}: market family {fam!r} is not mapped for BetR"


def _match_entain_leg(leg: dict, markets: dict, entrants: dict, home: str, away: str) -> dict | str:
    """One board leg -> {market_id, entrant_id}, or a reason string.

    Entain's tables are keyed by uuid, the line lives on the MARKET as `handicap`, and
    entrants carry `home_away` — so sides are matched on that rather than on team-name
    text, which is the one thing in this file that cannot drift.

    Only markets flagged `same_game_multi_available` are considered. The pricer IGNORES
    its own flag — 12 of 14 markets marked unavailable priced anyway — so honouring it
    here is the client-side half of that defence.
    """
    fam, sel = str(leg.get("market") or ""), str(leg.get("selection") or "")
    line, label = leg.get("line"), str(leg.get("label") or f"{leg.get('market')}:{leg.get('selection')}")

    def sgm_markets(name: str) -> list[dict]:
        return [m for m in markets.values()
                if _norm(m.get("name", "")) == name and m.get("same_game_multi_available")
                and m.get("visible", True)]

    def side(m: dict, want: str) -> dict | None:
        for e in entrants.values():
            if e.get("market_id") != m["id"] or not e.get("visible", True):
                continue
            if want in ("home", "away") and str(e.get("home_away", "")).lower() == want:
                return e
            if want not in ("home", "away") and _norm(e.get("name", "")) == want:
                return e
        return None

    def hit(m: dict, e: dict) -> dict:
        return {"market_id": m["id"], "entrant_id": e["id"]}

    if fam == "h2h":
        for m in sgm_markets("match betting"):
            e = side(m, sel if sel in ("home", "away") else _norm(sel))
            if e:
                return hit(m, e)
        return f"{label}: no open head-to-head selection at Entain"

    if fam == "total":
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"
        for m in sgm_markets("total points"):
            if abs(float(m.get("handicap") or 0) - float(line)) > 1e-9:
                continue
            e = side(m, sel)
            if e:
                return hit(m, e)
        return f"{label}: Entain has no open total at line {line}"

    if fam == "line":
        # `handicap` is stated from ONE side (-5.5 on a market whose entrants are both
        # teams), so the magnitude is matched and the SIDE comes from home_away. Asserting
        # which team owns the negative number would be a guess, and a wrong guess here
        # quotes the opposite bet at a plausible price.
        for m in sgm_markets("line"):
            if line is not None and abs(abs(float(m.get("handicap") or 0)) - abs(float(line))) > 1e-9:
                continue
            e = side(m, sel)
            if e:
                return hit(m, e)
        return f"{label}: Entain has no matching line market at {line}"

    return f"{label}: market family {fam!r} is not mapped for Entain"


async def _resolve(session: AsyncSession, book: str, fixture_id: str) -> tuple[Any, str, str] | str:
    """The linked event plus the fixture's team names, or a reason string."""
    ev = await _linked_event(session, fixture_id, book)
    if ev is None:
        return f"no {book} event linked to this fixture"
    f = (await session.execute(select(Fixture).where(Fixture.id == ev.fixture_id))).scalar()
    home, away = [*f.name.split(" v ", 1), ""][:2] if f and " v " in f.name else ("", "")
    return ev, home, away


async def quote_pointsbet(session: AsyncSession, mcp: Any, fixture_id: str,
                          legs: list[dict]) -> dict[str, Any]:
    got = await _resolve(session, "pointsbet", fixture_id)
    if isinstance(got, str):
        return {"unavailable": got}
    ev, home, away = got
    try:
        event = await mcp.call_tool("pointsbet_event", {"eventKey": int(ev.external_id)})
    except Exception as exc:
        return {"unavailable": f"pointsbet_event failed: {exc}"}
    markets = (event or {}).get("fixedOddsMarkets") or []
    if not markets:
        return {"unavailable": "PointsBet returned no markets for this event"}

    picked: list[Any] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_pointsbet_leg(leg, markets, home, away)
        (picked if isinstance(hit, dict) else misses).append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses}

    try:
        r = await mcp.call_tool("pointsbet_sgm_price",
                                {"eventKey": str(ev.external_id), "selectedOutcomes": picked})
    except Exception as exc:
        # PointsBet refuses in HTTP 200 with price 0; the MCP engine raises that for us.
        return {"unavailable": f"PointsBet refused to price this combination: {exc}"}

    decimal = _decimal_from("pointsbet", (r or {}).get("price"))
    if decimal is None:
        return {"unavailable": f"unexpected pricer response: {r!r}"}
    return {
        "priced_by": "pointsbet",
        "book_odds": round(decimal, 2),
        "legs_matched": len(picked),
        "warnings": ["PointsBet collapses a leg another leg implies and does NOT say so — "
                     "this price is for the legs listed, verify before betting"],
    }


async def quote_unibet(session: AsyncSession, mcp: Any, fixture_id: str,
                       legs: list[dict]) -> dict[str, Any]:
    got = await _resolve(session, "unibet", fixture_id)
    if isinstance(got, str):
        return {"unavailable": got}
    ev, home, away = got
    try:
        book = await mcp.call_tool("unibet_kambi_call",
                                   {"operation": "event_betoffer",
                                    "path_params": {"eventId": int(ev.external_id)}})
    except Exception as exc:
        return {"unavailable": f"unibet_kambi_call(event_betoffer) failed: {exc}"}
    offers = (book or {}).get("betOffers") or []
    if not offers:
        return {"unavailable": "Unibet returned no bet offers for this event"}

    ids: list[int] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_unibet_leg(leg, offers, home, away)
        if isinstance(hit, int):
            ids.append(hit)
        else:
            misses.append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses}

    try:
        r = await mcp.call_tool("unibet_sgm_price",
                                {"eventId": int(ev.external_id),
                                 "outcomeIds": ",".join(str(i) for i in ids)})
    except Exception as exc:
        # Kambi refuses with a real 400/409 and a typed body, which the engine raises.
        return {"unavailable": f"Unibet refused to price this combination: {exc}"}

    odds = (r or {}).get("selectedOdds")
    if not odds:
        # A single leg, or duplicates collapsed to one, returns a body with NO price key.
        return {"unavailable": "Unibet returned no combined price for these legs"}
    decimal = _decimal_from("unibet", odds.get("decimal"))
    if decimal is None:
        return {"unavailable": f"unexpected pricer response: {r!r}"}

    out: dict[str, Any] = {
        "priced_by": "unibet",
        "book_odds": round(decimal, 2),
        "legs_matched": len(ids),
        # The only book of the seven that says what it actually priced. Use it.
        "legs_priced": r.get("selectedOutcomeIds"),
    }
    if decimal >= 1001.0:
        out["warnings"] = ["1001.0 is Kambi's payout CEILING, not a quote — the true price "
                           "is longer and this is not the edge it looks like"]
    if len(r.get("selectedOutcomeIds") or ids) != len(ids):
        out.setdefault("warnings", []).append(
            f"Unibet priced {len(r['selectedOutcomeIds'])} legs, not the {len(ids)} sent — "
            "duplicate ids are deduplicated silently")
    return out


async def compare(session: AsyncSession, mcp: Any, fixture_id: str, legs: list[dict],
                  *, books: tuple[str, ...] = BOOKMAKERS) -> dict[str, Any]:
    """Quote the SAME legs at every book that will price them, side by side.

    This is the thing no single book's app can show you: one combination, priced by each
    book's own correlation model, on comparable units. The books disagree by more than the
    vig — measured live, correlation adjustments ran from -41% to +5% on the same anchor
    leg — so the spread between books on one combination is a real number, not noise.

    Books are quoted CONCURRENTLY. One book being slow, down, or refusing must not decide
    whether the others get asked, and a comparator that stops at the first refusal would
    show one price and call it the market.

    Every quote is restated with the legs that produced it. Four of the seven pricers can
    return a price for a DIFFERENT bet than the one requested — TAB and PointsBet and
    Entain by silently collapsing a leg another leg implies, BetR by honouring a price the
    caller supplied — and only Unibet echoes back what it actually priced. So the legs
    travel with the answer rather than being assumed from the request.
    """
    async def one(book: str) -> tuple[str, dict[str, Any]]:
        try:
            return book, await quote(session, mcp, book, fixture_id, legs)
        except Exception as exc:  # a resolver bug must not take the whole board down
            return book, {"unavailable": f"{book} quoting raised {type(exc).__name__}: {exc}"}

    results = dict(await asyncio.gather(*(one(b) for b in books)))

    priced = {b: r for b, r in results.items() if r.get("book_odds")}
    unavailable = {b: r.get("unavailable", "no price") for b, r in results.items()
                   if not r.get("book_odds")}

    ranked = sorted(priced.items(), key=lambda kv: kv[1]["book_odds"], reverse=True)
    out: dict[str, Any] = {
        "legs": legs,                      # restated, always — see the docstring
        "quotes": [dict(q, book=b) for b, q in ranked],
        "unavailable": unavailable,
        "books_priced": len(ranked),
    }
    if ranked:
        best_book, best = ranked[0]
        out["best"] = {"book": best_book, "book_odds": best["book_odds"]}
        if len(ranked) > 1:
            worst = ranked[-1][1]["book_odds"]
            out["spread_pct"] = round((best["book_odds"] / worst - 1) * 100, 2)
            out["note"] = (
                f"{best_book} pays {out['spread_pct']}% more than "
                f"{ranked[-1][0]} for the same {len(legs)} legs")
    if not ranked:
        out["note"] = "no book would price this combination"
    return out


async def quote_betr(session: AsyncSession, mcp: Any, fixture_id: str,
                     legs: list[dict]) -> dict[str, Any]:
    got = await _resolve(session, "betr", fixture_id)
    if isinstance(got, str):
        return {"unavailable": got}
    ev, home, away = got
    try:
        master = await mcp.call_tool("betr_master_event",
                                     {"MasterEventId": int(ev.external_id)})
    except Exception as exc:
        return {"unavailable": f"betr_master_event failed: {exc}"}
    events = (master or {}).get("Events") or []
    if not events:
        return {"unavailable": "BetR returned no market groups for this event"}

    picked: list[Any] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_betr_leg(leg, events, home, away)
        (picked if isinstance(hit, dict) else misses).append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses}
    if len(picked) < 2:
        return {"unavailable": "BetR will not price fewer than two legs (ErrorNo 4500)"}

    try:
        r = await mcp.call_tool("betr_sgm_price",
                                {"MasterEventID": int(ev.external_id), "Markets": picked})
    except Exception as exc:
        # BetR refuses in HTTP 200 with Price 0 + ErrorNo; the engine raises that.
        return {"unavailable": f"BetR refused to price this combination: {exc}"}

    decimal = _decimal_from("betr", (r or {}).get("Price"))
    if decimal is None:
        return {"unavailable": f"unexpected pricer response: {r!r}"}
    return {
        "priced_by": "betr",
        "book_odds": round(decimal, 2),
        "legs_matched": len(picked),
        "warnings": ["BetR refuses a redundant leg rather than dropping one, so this price "
                     "is for the legs listed"],
    }


async def quote_entain(session: AsyncSession, mcp: Any, fixture_id: str,
                       legs: list[dict]) -> dict[str, Any]:
    got = await _resolve(session, "entain", fixture_id)
    if isinstance(got, str):
        return {"unavailable": got}
    ev, home, away = got
    try:
        card = await mcp.call_tool("entain_sport_event_card", {"id": str(ev.external_id)})
    except Exception as exc:
        return {"unavailable": f"entain_sport_event_card failed: {exc}"}
    markets = (card or {}).get("markets") or {}
    entrants = (card or {}).get("entrants") or {}
    if not markets:
        return {"unavailable": "Entain returned no markets for this event"}

    picked: list[Any] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_entain_leg(leg, markets, entrants, home, away)
        (picked if isinstance(hit, dict) else misses).append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses}

    eid = str(ev.external_id)
    try:
        r = await mcp.call_tool("entain_sgm_price", {
            "same_game_multies": {eid: {"event_id": eid, "selections": picked}}})
    except Exception as exc:
        return {"unavailable": f"Entain refused to price this combination: {exc}"}

    entry = ((r or {}).get("prices") or {}).get(eid) or {}
    if not entry.get("available"):
        clash = entry.get("conflicting_selections")
        return {"unavailable": "Entain will not combine these legs",
                **({"conflicting": clash} if clash else {})}
    decimal = _decimal_from("entain", entry.get("odds"))
    if decimal is None:
        return {"unavailable": f"unexpected pricer response: {r!r}"}
    return {
        "priced_by": "entain",
        "book_odds": round(decimal, 2),
        "legs_matched": len(picked),
        # Measured live: 5 of 41 two-entrant markets priced a pair that CANNOT both win.
        "warnings": ["Entain's `available: true` is not proof the combination is coherent "
                     "— it quotes some impossible pairs at long odds"],
    }


#: Our sport slug -> TAB's own (sport, competition) names. TAB addresses matches by NAME,
#: not by id, so this table is what stands in for an id mapping. Adding a sport is adding
#: a row; a sport that is missing reports that plainly rather than guessing a name.
TAB_NAMES = {
    "afl": ("AFL Football", "AFL"),
    "australian_rules": ("AFL Football", "AFL"),
    "nrl": ("Rugby League", "NRL"),
    "rugby_league": ("Rugby League", "NRL"),
    "nba": ("Basketball", "NBA"),
    "basketball": ("Basketball", "NBA"),
}


def _tab_team_matches(prop_name: str, team: str) -> bool:
    """Does a TAB proposition name refer to this team?

    TAB abbreviates: "Wst Bulldogs" for Western Bulldogs, "Sydney" for Sydney Swans. An
    exact comparison fails on most of the league, so the test is whether the team's
    longest distinctive word appears — "bulldogs" is in "Wst Bulldogs", "collingwood" in
    "Collingwood". Short words are skipped so "st" out of "St Kilda" cannot match half the
    competition.
    """
    pn = _norm(prop_name)
    words = [w for w in _norm(team).split() if len(w) >= 4]
    if not words:
        return _norm(team) in pn
    return max(words, key=len) in pn


def _match_tab_leg(leg: dict, markets: list[dict], home: str, away: str) -> int | str:
    """One board leg -> a TAB propositionId, or a reason string.

    VERIFIED live 2026-08-27 against AFL Wst Bulldogs v Collingwood (174 markets, 53
    combinable). TAB's market names are heavily abbreviated and carry the fixture in them
    — "AFL WBdg-Coll Hd to Hd", "AFL WBdg-Coll TotPtsOU 169.5", "AFL WBdg-Coll Line +1.5"
    — so markets are found by TOKEN rather than by a readable name, and the line is read
    out of the market name.

    Only markets flagged `sameGame` can be combined (53 of 174 here). The per-quarter line
    markets are the instructive counter-example: they look identical to the match line and
    are NOT combinable, so the flag is the only thing separating them.
    """
    fam, sel = str(leg.get("market") or ""), str(leg.get("selection") or "")
    line, label = leg.get("line"), str(leg.get("label") or f"{leg.get('market')}:{leg.get('selection')}")

    def combinable(m: dict) -> bool:
        return bool(m.get("sameGame")) and _norm(m.get("bettingStatus", "")) == "open"

    def props(m: dict) -> list[dict]:
        return [p for p in (m.get("propositions") or [])
                if p.get("id") is not None and p.get("isOpen", True)]

    want_team = home if sel == "home" else away if sel == "away" else sel
    cands = [m for m in markets if combinable(m)]

    if fam == "h2h":
        for m in cands:
            if "hd to hd" not in _norm(m.get("name", "")):
                continue
            for p in props(m):
                if _tab_team_matches(p.get("name", ""), want_team):
                    return int(p["id"])
        return f"{label}: no combinable head-to-head market at TAB"

    if fam == "total":
        if line is None or sel not in ("over", "under"):
            return f"{label}: totals need a line and an over/under side"
        for m in cands:
            # The line is IN the market name: "TotPtsOU 169.5".
            if "totptsou" not in _norm(m.get("name", "")) or str(line) not in str(m.get("name", "")):
                continue
            for p in props(m):
                if _norm(p.get("name", "")).startswith(sel):
                    return int(p["id"])
        return f"{label}: TAB has no combinable total at line {line}"

    if fam == "line":
        for m in cands:
            nm = _norm(m.get("name", ""))
            # "line +1.5" as a whole word — not "1stqtrln"/"2ndqline", which are
            # per-period and excluded by the sameGame flag anyway.
            if "line" not in nm.split():
                continue
            if line is not None and str(abs(float(line))) not in str(m.get("name", "")):
                continue
            for p in props(m):
                if _tab_team_matches(p.get("name", ""), want_team):
                    return int(p["id"])
        return f"{label}: TAB has no combinable line market at {line}"

    return f"{label}: market family {fam!r} is not mapped for TAB"


async def quote_tab(session: AsyncSession, mcp: Any, fixture_id: str,
                    legs: list[dict]) -> dict[str, Any]:
    """TAB, addressed by NAME rather than by id.

    Ingestion keeps only a slug, so instead of threading TAB's names through storage this
    lists TAB's own matches for the competition and matches the fixture against them. The
    fixture name is already "X v Y", which is TAB's own shape.
    """
    ev = await _linked_event(session, fixture_id, "tab")
    fx_id = ev.fixture_id if ev is not None else None
    f = None
    if fx_id is not None:
        f = (await session.execute(select(Fixture).where(Fixture.id == fx_id))).scalar()
    if f is None:
        try:
            f = (await session.execute(
                select(Fixture).where(Fixture.id == uuid.UUID(fixture_id)))).scalar()
        except ValueError:
            f = None
    if f is None or " v " not in (f.name or ""):
        return {"unavailable": "no fixture name to match against TAB's own match names"}
    home, away = f.name.split(" v ", 1)

    slug = _norm(getattr(f, "sport", "") or "").replace(" ", "_")
    names = TAB_NAMES.get(slug)
    if names is None:
        return {"unavailable": f"no TAB sport/competition mapping for {slug!r} — add a row "
                               f"to TAB_NAMES (have: {', '.join(sorted(TAB_NAMES))})"}
    sport, competition = names

    try:
        comp = await mcp.call_tool("tab_competition",
                                   {"sport": sport, "competition": competition})
    except Exception as exc:
        return {"unavailable": f"tab_competition failed: {exc}"}

    want = {_norm(home), _norm(away)}
    match_name = None
    for m in (comp or {}).get("matches") or []:
        nm = str(m.get("name") or "")
        if " v " not in nm:
            continue
        a, b = (t.strip() for t in nm.split(" v ", 1))
        # Both teams must appear, in either order — TAB sometimes lists home second.
        if {_norm(a), _norm(b)} == want or all(
                any(_norm(t) in _norm(x) or _norm(x) in _norm(t) for x in (a, b)) for t in (home, away)):
            match_name = nm
            break
    if match_name is None:
        return {"unavailable": f"no TAB match in {competition} named for {home} v {away}"}

    try:
        mk = await mcp.call_tool("tab_match_markets",
                                 {"sport": sport, "competition": competition, "match": match_name})
    except Exception as exc:
        return {"unavailable": f"tab_match_markets failed: {exc}"}
    markets = (mk or {}).get("markets") or []
    if not markets:
        return {"unavailable": f"TAB returned no markets for {match_name!r}"}

    ids: list[int] = []
    misses: list[Any] = []
    for leg in legs:
        hit = _match_tab_leg(leg, markets, home, away)
        if isinstance(hit, int):
            ids.append(hit)
        else:
            misses.append(hit)
    if misses:
        return {"unavailable": "could not match every leg", "unmatched": misses,
                "tab_match": match_name}

    try:
        r = await mcp.call_tool("tab_sgm_price", {
            "bets": [{"type": "FIXED_ODDS", "legs": [
                {"type": "SAME_GAME_MULTI",
                 "propositions": [{"type": "WIN", "propositionId": i} for i in ids]}]}],
            "returnValidationMatrix": True})
    except Exception as exc:
        return {"unavailable": f"TAB refused to price this combination: {exc}"}

    bets = (r or {}).get("bets") or []
    leg0 = ((bets[0] if bets else {}).get("legs") or [{}])[0]
    decimal = _decimal_from("tab", (leg0.get("odds") or {}).get("decimal"))
    if decimal is None:
        return {"unavailable": f"unexpected pricer response: {r!r}"}

    out: dict[str, Any] = {"priced_by": "tab", "book_odds": round(decimal, 2),
                           "legs_matched": len(ids), "tab_match": match_name}
    # TAB is the one book that SAYS which legs it dropped. Use it rather than assume.
    dropped = [p for p in (leg0.get("redundantPropositions") or [])
               if _norm(p.get("status", "")) == "redundant"]
    if dropped:
        out["warnings"] = [
            f"TAB dropped {len(dropped)} redundant leg(s) and priced the rest — this quote "
            f"is NOT for the {len(ids)} legs requested"]
        out["legs_dropped"] = dropped
    return out


#: book -> its quoter. Registered here so `quote` cannot silently fall through to
#: Sportsbet for a book whose resolver was never written — the bug that would put one
#: book's price under another book's name.
_QUOTERS = {
    "sportsbet": lambda *a: quote_sportsbet(*a),
    "pointsbet": lambda *a: quote_pointsbet(*a),
    "unibet": lambda *a: quote_unibet(*a),
    "betr": lambda *a: quote_betr(*a),
    "entain": lambda *a: quote_entain(*a),
    "tab": lambda *a: quote_tab(*a),
}


async def quote(session: AsyncSession, mcp: Any, bookmaker: str, fixture_id: str,
                legs: list[dict]) -> dict[str, Any]:
    if bookmaker not in BOOKMAKERS:
        return {"unavailable": f"unknown bookmaker {bookmaker!r} (have: {', '.join(BOOKMAKERS)})"}
    if mcp is None:
        return {"unavailable": "live data plane not running — book quotes need the in-process poller"}
    return await _QUOTERS[bookmaker](session, mcp, fixture_id, legs)
