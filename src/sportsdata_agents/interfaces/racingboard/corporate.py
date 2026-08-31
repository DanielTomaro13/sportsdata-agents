"""The five books, each as both a PRICE source and a DISCOVERY source.

Every book indexes its own day of racing and prices any race in it. That the index is
also a discovery source is the point: the board used to build its race list from TAB
alone, which is the narrowest catalogue of the five (413 races against PointsBet's
1,200), so a race TAB did not carry could never appear no matter how many books priced
it. Here each book contributes its races to the union spine in `spine.py`.

Measured live 2026-08-31, one day, all three codes:

    book        R     G     H    total     per-race price call
    PointsBet   534   435   231   1200     0.05s
    Ladbrokes   347   263   100    710     0.08s
    Sportsbet   289   237    95    621     0.26s   (the slow one)
    TAB         204   135    74    413     0.06s
    Dabble       71    39    27    137 MEETINGS, 201 open races

Two of the old docstring's claims were wrong and are worth recording as wrong.
Ladbrokes does NOT "404 without auth" — its public racecard answers anonymously, 710
races, zero errors. And Dabble is not "too heavy for fast polling": it is
MEETING-GRANULAR, so one call returns a whole meeting's races with prices embedded,
making it the cheapest book here rather than the dearest.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .config import settings
from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot
from .venues import norm_runner, norm_venue, unique_venue_match

# ─── per-book code mapping (verified live 2026-08-31) ───────────────────
#
# Every book spells the three codes differently, and a wrong mapping silently drops a
# whole discipline rather than erroring — which is how harness came to look empty.

PB_TYPE_TO_CODE = {1: "R", 2: "H", 3: "G", 4: "G"}

#: Ladbrokes/Neds category UUIDs, read off the live payload 2026-08-31 and identified by
#: the meetings behind each:
#:
#:   4a2788f8  38 meetings  347 races   Corowa, Ballarat Synthetic, Taree   -> thoroughbred
#:   9daef0d7  22 meetings  263 races   Sandown Park, Romford Bags, Shepparton -> greyhound
#:   161d9be2  10 meetings  100 races   Solvalla, Globe Derby, Northfield Park -> harness
#:
#: Copied from a TRUNCATED display the first time and two of the three were wrong, which
#: cost the whole book: an unrecognised category maps to no code and its races are
#: dropped in silence. `build_index` therefore reports unknown categories rather than
#: skipping them quietly — that is the only thing standing between an upstream id change
#: and a discipline disappearing from the board.
ENTAIN_CATEGORY_TO_CODE = {
    "4a2788f8-e825-4d36-9894-efd4baf1cfae": "R",
    "9daef0d7-bf3c-4f50-921d-8e818c60fe61": "G",
    "161d9be2-e909-4326-8c2c-35ed71fb460b": "H",
}

#: Dabble exposes racing ONLY through the active-competitions feed's `sportName`. Its
#: /sports catalogue lists a single "Horse Racing" entry, no Greyhound and no Harness,
#: and reports isRacing:false on all 24 of its sports — so filtering discovery by a
#: racing sportId returns nothing at all. These three names do not appear in /sports.
DABBLE_SPORT_TO_CODE = {
    "Thoroughbred Racing": "R",
    "Greyhound Racing": "G",
    "Harness Racing": "H",
}


def _sb_code(class_name: str) -> str:
    """Sportsbet's `className`, which splits each code across several values:
    `Horses - Aus/NZ`, `Horses - International`, `Horses - Asia`, `Greyhound Racing`,
    `Harness Racing`, `Harness Racing - International`."""
    n = (class_name or "").lower()
    if "greyhound" in n:
        return "G"
    if "harness" in n:
        return "H"
    return "R"


def _epoch(value: Any) -> float | None:
    """Advertised start as epoch seconds, from whatever shape the book uses.

    Sportsbet sends an INTEGER epoch (1788145200) where the others send ISO. Parsing
    only ISO returned None for every Sportsbet race, and a race with no start cannot be
    told apart from the second meeting at the same track — so this silently cost a
    third of the board's coverage rather than raising anything.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Milliseconds if it is far too large to be seconds.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class BookRace:
    """One race as ONE book sees it — its own venue spelling and its own handle.

    `race_no` is optional because Dabble does not publish one: its fixtures carry a
    race NAME ("Sportsbet Final") and an `advertisedStart`, nothing numbering them
    within the meeting. `start` therefore has to be a first-class join key rather than
    decoration, or Dabble contributes nothing.
    """

    code: str
    venue: str              # as this book spells it
    race_no: int | None
    start: float | None     # epoch seconds
    handle: Any             # whatever this book needs to price it
    name: str = ""          # the race's own name, for the board's label
    #: TAB's meeting location -- an AU state (NSW/VIC/...) or a country code. Only
    #: TAB publishes it, so it is empty for a race TAB does not carry. Downstream
    #: this gates the tote blend, which is only meaningful where a TAB pool exists
    #: in the first place, so the two absences line up exactly.
    location: str = ""

    @property
    def venue_key(self) -> str:
        return norm_venue(self.venue)


class CorporateBook:
    """One book: indexes a day's races, then prices any of them."""

    name = "book"

    def __init__(self) -> None:
        self.races: list[BookRace] = []

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    # ---- lookup ----

    def handle_for(self, race: RaceRef) -> Any | None:
        """This book's handle for a canonical race, or None if it does not have it.

        Venue resolution is by `unique_venue_match` against this book's OWN spellings,
        so `MOHAWK` reaches `Woodbine Mohawk Park` while `WOODBINE` still resolves to
        `Woodbine`. Code and race number are independent gates on top.

        THE START TIME IS PART OF THE IDENTITY, not a tiebreaker of last resort.
        `(code, venue, race_no)` is not unique: a track can hold two meetings in one
        day — Townsville ran a day and a night greyhound card, each with its own race 1,
        and Woodbine Mohawk Park does the same — so PointsBet carried 121 such pairs and
        Sportsbet 20. Matching on the triple alone found two candidates, refused as
        ambiguous, and dropped the race from that book entirely. That is why PointsBet,
        the biggest catalogue of the five, was covering only half the board.
        """
        target = race.start_epoch
        same_code = [r for r in self.races if r.code == race.code]
        if not same_code:
            return None

        def by_start(cands: list[BookRace]) -> Any | None:
            if not cands:
                return None
            # A lone candidate is NOT automatically the answer. A book listing
            # tomorrow's card as well as today's has exactly one race 8 at Northfield
            # Park once today's has run — accepting it unchecked would price a race 20
            # hours away and look entirely normal doing it.
            if target is None:
                return cands[0].handle if len(cands) == 1 else None
            near = [c for c in cands if c.start is None
                    or abs(c.start - target) <= settings.start_tolerance_seconds]
            if not near:
                return None
            if len(near) == 1:
                return near[0].handle
            # Several within tolerance: nearest wins, and a genuine tie refuses.
            near = [c for c in near if c.start is not None]
            if not near:
                return None
            # Nearest start wins. Two cards at one track are hours apart, so this is a
            # wide margin rather than a fine judgement; still refuse a genuine tie.
            near.sort(key=lambda c: abs((c.start or 0) - target))
            if len(near) > 1 and abs((near[0].start or 0) - target) == abs((near[1].start or 0) - target):
                return None
            return near[0].handle

        numbered = [r for r in same_code if r.race_no == race.race_no]
        if numbered:
            # All this book's races at the one track with this number — possibly the
            # two meetings above, which the start then separates.
            venues = {r.venue for r in numbered}
            picked = unique_venue_match(race.venue, [(v, v) for v in venues])
            if picked is not None:
                got = by_start([r for r in numbered if r.venue == picked])
                if got is not None:
                    return got

        # Books that publish no race number at all (Dabble): the start IS the key.
        unnumbered = [r for r in same_code if r.race_no is None and r.start is not None]
        if not unnumbered or target is None:
            return None
        venues = {r.venue for r in unnumbered}
        picked = unique_venue_match(race.venue, [(v, v) for v in venues])
        if picked is None:
            return None
        return by_start([r for r in unnumbered if r.venue == picked])


# ─── PointsBet ──────────────────────────────────────────────────────────


class PointsbetBook(CorporateBook):
    name = "pointsbet"

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call(
            "pointsbet_racing_meetings",
            startDate=date + "T00:00:00.000Z",
            endDate=date + "T23:59:59.000Z",
        )
        races: list[BookRace] = []
        for group in data or []:
            for mt in group.get("meetings", []):
                code = PB_TYPE_TO_CODE.get(mt.get("racingType"))
                if not code:
                    continue
                venue = mt.get("venue", "")
                for ra in mt.get("races", []):
                    rno = ra.get("raceNumber")
                    if rno is None:
                        continue
                    races.append(BookRace(
                        code=code, venue=venue, race_no=int(rno),
                        start=_epoch(ra.get("advertisedStartDateTimeUtc")),
                        handle=ra.get("raceId"), name=ra.get("name", ""),
                    ))
        self.races = races

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        rc = await engine.try_call("pointsbet_racing_race", raceId=handle)
        out: dict[str, dict[str, Any]] = {}
        for rn in (rc or {}).get("runners", []):
            if rn.get("isScratched"):
                continue
            fl = rn.get("fluctuations") or {}
            cur = fl.get("current")
            if cur:
                out[norm_runner(rn.get("runnerName", ""))] = {
                    "price": cur, "open": fl.get("open"),
                    "name": rn.get("runnerName", ""), "number": rn.get("runnerNumber")}
        return out


# ─── Sportsbet ──────────────────────────────────────────────────────────


class SportsbetBook(CorporateBook):
    name = "sportsbet"

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call("sportsbet_racing_allracing", eventDate=date)
        races: list[BookRace] = []
        for d in (data or {}).get("dates", []):
            for sec in d.get("sections", []):
                for mt in sec.get("meetings", []):
                    code = _sb_code(mt.get("className", ""))
                    venue = mt.get("name", "")
                    for ev in mt.get("events", []):
                        rno = ev.get("raceNumber")
                        if rno is None:
                            continue
                        races.append(BookRace(
                            code=code, venue=venue, race_no=int(rno),
                            start=_epoch(ev.get("startTime")),
                            handle=ev.get("id"), name=ev.get("name", ""),
                        ))
        self.races = races

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        rc = await engine.try_call("sportsbet_racecard", eventId=handle)
        out: dict[str, dict[str, Any]] = {}
        markets = (rc or {}).get("markets", [])
        win = next((m for m in markets if "win" in (m.get("name", "").lower())), None)
        if win is None:
            win = markets[0] if markets else None
        if win is None:
            return out
        for s in win.get("selections", []):
            if s.get("isOut"):
                continue
            flucs = s.get("recentOddsFluctuations") or []
            price = flucs[0] if flucs else None
            if price:
                out[norm_runner(s.get("name", ""))] = {
                    "price": price, "open": flucs[-1] if flucs else None,
                    "name": s.get("name", ""), "number": s.get("runnerNumber")}
        return out


# ─── Ladbrokes / Neds (Entain) ──────────────────────────────────────────


class LadbrokesBook(CorporateBook):
    """710 races a day, 0.08s per racecard, anonymous.

    The old note that this "404s without auth" was stale — it was kept in the docstring
    long enough to keep the second-largest catalogue on the board out of it entirely.
    """

    name = "ladbrokes"

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call(
            "entain_racing_meeting", date=date, timezone="Australia/Sydney"
        )
        meetings = (data or {}).get("meetings") or {}
        all_races = (data or {}).get("races") or {}

        races: list[BookRace] = []
        unknown: dict[str, str] = {}
        for mt in meetings.values():
            cid = mt.get("category_id")
            code = ENTAIN_CATEGORY_TO_CODE.get(cid)
            if not code:
                # Say so. A silently dropped category is a whole discipline vanishing
                # from the board with nothing to show for it.
                if cid and cid not in unknown:
                    unknown[cid] = mt.get("name", "?")
                continue
            venue = mt.get("name", "")
            for rid in (mt.get("race_ids") or []):
                ra = all_races.get(rid) or {}
                # `number`, not `race_number` — the wrong key here indexed zero races
                # while looking exactly like a book with nothing on today.
                rno = ra.get("number")
                if rno is None:
                    continue
                races.append(BookRace(
                    code=code, venue=venue, race_no=int(rno),
                    start=_epoch(ra.get("advertised_start")), handle=rid,
                    name=ra.get("name", ""),
                ))
        if unknown:
            print(f"[ladbrokes] unmapped category ids (races DROPPED): "
                  + ", ".join(f"{c} e.g. {n!r}" for c, n in unknown.items()))
        self.races = races

    #: A real bookmaker's win margin. The floor matters more than it looks: an
    #: earlier version accepted 1.05 and preferred the TIGHTEST book, which is
    #: backwards -- nobody offers a 5% margin, so that rule reliably picked
    #: whichever derived product happened to look least like a bookmaker. On
    #: Marburg R4 it published Ideal Tiger at $6.00 against a real price of $1.95,
    #: off a set summing to 1.052. The genuine win line there summed to 1.308.
    WIN_OVERROUND = (1.10, 1.70)

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        """Entain's fixed-win line for one race.

        Two things about this payload are not what they look like.

        The racecard is WRAPPED: entrants and prices live under `data`, not at the
        top level. Reading them from the root returns empty for every race, which is
        indistinguishable from a book that simply has no prices — Ladbrokes indexed
        710 races a day and contributed a price to none of them.

        And `prices` is keyed `<entrant_id>:<product_type_id>:`, not by entrant, with
        ~84 products per runner and the odds under `odds.numerator/denominator` —
        not `win_numerator`. Entain never names the products in this response, so the
        win line has to be recognised by its shape rather than looked up: take the
        products that price the WHOLE field, keep those whose implied probabilities
        sum inside a real book's margin, and prefer the tightest. That last tiebreak
        is what makes it stable — the primary fixed-win line is the sharpest one
        Entain publishes for a race; the looser siblings are derived products.
        """
        rc = await engine.try_call("entain_racing_racecard", method="racecard", id=handle)
        data = (rc or {}).get("data") or {}
        entrants = {eid: en for eid, en in (data.get("entrants") or {}).items()
                    if not (en.get("is_scratched") or en.get("scratched_time"))}
        if not entrants:
            return {}

        by_product: dict[str, dict[str, float]] = {}
        for key, value in (data.get("prices") or {}).items():
            parts = key.split(":")
            if len(parts) < 2 or parts[0] not in entrants:
                continue
            odds = (value or {}).get("odds") or {}
            num, den = odds.get("numerator"), odds.get("denominator")
            # Entain quotes fractional: decimal = num/den + 1. Dropping the +1 would
            # understate every price on the board.
            if not (isinstance(num, (int, float)) and isinstance(den, (int, float)) and den):
                continue
            by_product.setdefault(parts[1], {})[parts[0]] = num / den + 1.0

        # `entrants` spans EVERY market on the race -- Final Field and Live Racing
        # both -- so "covers the whole field" against all of them matches nothing,
        # and the products that do span the lot are place and each-way books, which
        # is why the plausible overrounds came out between 2.3 and 4.3. Candidates
        # are judged per market instead: a win line prices one market completely.
        fields: dict[str, set[str]] = {}
        for eid, en in entrants.items():
            fields.setdefault(str(en.get("market_id") or ""), set()).add(eid)

        # The race's field is the BIGGEST market, not any market. Ranking candidates
        # across markets by overround silently prefers the smallest one, because a
        # shorter field sums to less by construction -- that priced a 13-entrant
        # Warrnambool race off a 7-runner book.
        field = max(fields.values(), key=len, default=set())
        if len(field) < 4:
            return {}

        # CONSENSUS, not the tightest book. Entain publishes the same win line under
        # many product ids -- nine of them carried Marburg R4's real prices -- while
        # the derived products (each-way legs, boosted specials, early lines) each
        # differ. So group the candidate books by the prices they actually quote and
        # take the line the most products agree on. That needs no product UUID, which
        # is the point: Entain never names them in this response, and a hard-coded id
        # would rot silently the day they reissue it.
        agree: dict[tuple, list[dict[str, float]]] = {}
        for quotes in by_product.values():
            covered = {eid: p for eid, p in quotes.items() if eid in field}
            if len(covered) != len(field):
                continue
            total = sum(1.0 / p for p in covered.values() if p > 1.0)
            if not (self.WIN_OVERROUND[0] <= total <= self.WIN_OVERROUND[1]):
                continue
            signature = tuple(sorted((eid, round(p, 2)) for eid, p in covered.items()))
            agree.setdefault(signature, []).append(covered)
        if not agree:
            return {}
        # Most-agreed wins; ties break toward the tighter book, which among genuine
        # win lines is the better price rather than a different kind of market.
        best_sig = max(agree, key=lambda k: (len(agree[k]),
                                             -sum(1.0 / p for _e, p in k if p > 1.0)))
        best = agree[best_sig][0]

        out: dict[str, dict[str, Any]] = {}
        for eid, price in best.items():
            en = entrants.get(eid) or {}
            name = en.get("name") or ""
            if price > 1.0 and name:
                out[norm_runner(name)] = {"price": round(price, 2), "open": None,
                                          "name": name,
                                          "number": en.get("number") or en.get("runner_number")}
        return out


# ─── Dabble ─────────────────────────────────────────────────────────────


class DabbleBook(CorporateBook):
    """Meeting-granular, and the cheapest book here because of it.

    One competition IS one meeting, and one fixtures call returns every race in it with
    markets, selections and prices embedded — so a full refresh of every Dabble price on
    the board costs ~137 requests where a per-race book costs ~700.

    Discovery must key on `sportName` from the ACTIVE-COMPETITIONS feed. The /sports
    catalogue lists no Greyhound and no Harness sport and reports isRacing:false on all
    24 of its entries, so anything that filters by a racing sportId finds nothing —
    which is exactly why Dabble looked horse-only and empty on the first pass.
    """

    name = "dabble"

    #: Racing win markets. Dabble leaves market NAMES null in the slim fixtures feed, so
    #: the win market has to be identified by `resultingType`. RacingSrm* is Same-Race-
    #: Multi and RacingDD* exotics — both must stay out of a win-price comparison.
    #: The fixed-odds win market, then the SP win market as a fallback. Exact names,
    #: not prefixes: the old "racingfixed"/"racingsp" prefixes also matched
    #: RacingFixedPlace and RacingSPPlace, so place prices were being mixed into the
    #: win line whenever anything matched at all.
    _WIN_TYPES = ("racingfixedwin", "racingspwin")

    def __init__(self) -> None:
        super().__init__()
        #: competition id -> the meeting's races, so a whole meeting is priced in one call.
        self._meeting_of: dict[Any, str] = {}

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call("dabble_active_competitions")
        payload = (data or {}).get("data") if isinstance(data, dict) else data
        comps = (payload or {}).get("activeCompetitions") if isinstance(payload, dict) else payload
        comps = comps or []

        racing = [(c, DABBLE_SPORT_TO_CODE[c["sportName"]])
                  for c in comps if c.get("sportName") in DABBLE_SPORT_TO_CODE]

        sem = asyncio.Semaphore(max(1, settings.book_concurrency))

        async def one(comp: dict, code: str) -> list[BookRace]:
            async with sem:
                fx = await engine.try_call("dabble_competition_fixtures",
                                           competitionId=comp["id"])
            items = (fx or {}).get("data") if isinstance(fx, dict) else fx
            out: list[BookRace] = []
            for f in items or []:
                # No race number anywhere in a Dabble fixture — the start time is the
                # join key. See BookRace.race_no.
                out.append(BookRace(
                    code=code, venue=comp.get("name", ""), race_no=None,
                    start=_epoch(f.get("advertisedStart")), handle=f.get("id"),
                ))
            return out

        gathered = await asyncio.gather(*(one(c, code) for c, code in racing))
        self.races = [r for chunk in gathered for r in chunk]

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        d = await engine.try_call("dabble_fixture_details", fixtureId=handle)
        det = (d or {}).get("sportFixtureDetail") or {}
        markets = det.get("markets") or []
        # Prefer the fixed-odds win market; fall back to SP win only if there is no
        # fixed one. Both exist on most races and they are different products.
        win_ids: set = set()
        for want in self._WIN_TYPES:
            win_ids = {m.get("id") for m in markets
                       if str(m.get("resultingType") or "").lower() == want}
            if win_ids:
                break
        if not win_ids:
            return {}

        # The MARKET LINK IS ON THE PRICE, not on the selection: a Dabble selection
        # carries only {id, name, isDisplayed}. The old code filtered selections by
        # `marketId`, a key they do not have, so the test never passed and Dabble
        # returned nothing for every race on the board -- present on the spine,
        # absent from every price grid.
        names = {sel.get("id"): (sel.get("name") or "")
                 for sel in (det.get("selections") or [])
                 if sel.get("isDisplayed") is not False}
        out: dict[str, dict[str, Any]] = {}
        for entry in (det.get("prices") or []):
            if entry.get("marketId") not in win_ids:
                continue
            name = names.get(entry.get("selectionId"))
            price = entry.get("price")
            if not name or not isinstance(price, (int, float)) or price <= 1.0:
                continue
            out[norm_runner(name)] = {"price": float(price), "open": None,
                                      "name": name, "number": None}
        return out


class TabBook(CorporateBook):
    """TAB as one contributor among several, rather than the board's spine.

    It still supplies the tote pool — which no corporate book has and which is half of
    what the board is for — but it no longer decides which races exist. Its handle is
    the `(raceType, venueMnemonic, raceNumber)` triple its own racecard route needs;
    that mnemonic is TAB's, not the board's identity.

    Kept at its spec rate limit while the corporate books run at full tilt: it is the
    one source here behind an authenticated Akamai handshake rather than an anonymous
    public feed, so hammering it is an account risk rather than a bandwidth question.
    """

    name = "tab"

    def __init__(self) -> None:
        super().__init__()
        self._date = ""

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        self._date = date
        data = await engine.try_call(
            "tab_racing_meetings", date=date, jurisdiction=settings.jurisdiction
        )
        races: list[BookRace] = []
        for m in (data or {}).get("meetings", []):
            code = m.get("raceType")
            if code not in settings.codes:
                continue
            venue, mnem = m.get("meetingName", ""), m.get("venueMnemonic", "")
            for ra in m.get("races", []):
                rno = ra.get("raceNumber")
                if rno is None:
                    continue
                races.append(BookRace(
                    code=code, venue=venue, race_no=int(rno),
                    start=_epoch(ra.get("raceStartTime")),
                    handle=(code, mnem, int(rno)), name=ra.get("raceName", ""),
                    location=m.get("location") or "",
                ))
        self.races = races

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        """TAB's fixed-odds win prices. The tote pool comes through `sources.tab_snapshot`,
        which owns the richer per-runner picture; this is only the price line."""
        code, mnem, rno = handle
        rc = await engine.try_call(
            "tab_racing_race", date=self._date, raceType=code,
            venueMnemonic=mnem, raceNumber=rno, jurisdiction=settings.jurisdiction,
        )
        out: dict[str, dict[str, Any]] = {}
        for r in (rc or {}).get("runners", []):
            if r.get("scratched"):
                continue
            odds = r.get("fixedOdds") or {}
            price = odds.get("returnWin")
            if price and price > 1.0:
                out[norm_runner(r.get("runnerName", ""))] = {
                    "price": price, "open": odds.get("returnWinOpen"),
                    "name": r.get("runnerName", ""), "number": r.get("runnerNumber")}
        return out


#: Every book the board can use, by name. `spine.py` and the poller work from this.
BOOKS: dict[str, type[CorporateBook]] = {
    "tab": TabBook,
    "pointsbet": PointsbetBook,
    "sportsbet": SportsbetBook,
    "ladbrokes": LadbrokesBook,
    "dabble": DabbleBook,
}


def build_books(names: list[str] | None = None) -> list[CorporateBook]:
    return [BOOKS[n]() for n in (names or settings.books) if n in BOOKS]


# ─── the source the poller drives ───────────────────────────────────────


class CorporateSource:
    """Holds the enabled books, refreshes their indices, and merges prices in."""

    def __init__(self, books: list[CorporateBook] | None = None) -> None:
        self.books = books if books is not None else build_books()
        self._cache: dict[str, dict[str, dict[str, float]]] = {}
        self._cache_by_number: dict[str, dict[int, dict[str, float]]] = {}
        self._last_fetch: dict[str, float] = {}

    async def refresh_indices(self, engine: SportsDataEngine, date: str) -> None:
        """Rebuild every book's index concurrently.

        The books are independent upstreams; serially this cost the SUM of their index
        latencies on a path that runs before any race can be priced. One book's failure
        must not touch the others, so each is caught individually rather than letting
        gather() abandon the set.
        """
        async def one(book: CorporateBook) -> None:
            try:
                await book.build_index(engine, date)
            except Exception as exc:
                print(f"[corporate] {book.name} index error: {exc}")

        await asyncio.gather(*(one(b) for b in self.books))

    async def enrich(self, engine: SportsDataEngine, race: RaceRef, snapshot: RaceSnapshot) -> None:
        """Fetch (throttled) and apply corporate prices onto a snapshot's runners."""
        now = time.time()
        due = now - self._last_fetch.get(race.race_key, 0) >= settings.corp_interval
        if due:
            merged: dict[str, dict[str, float]] = {}

            # Books are priced CONCURRENTLY: a race costs the slowest book rather than
            # the sum of all of them. Bounded by book_concurrency so adding books cannot
            # multiply the instantaneous rate against any single upstream.
            sem = asyncio.Semaphore(max(1, settings.book_concurrency))

            async def price_one(book: CorporateBook) -> tuple[str, dict] | None:
                handle = book.handle_for(race)
                if handle is None:
                    return None
                async with sem:
                    try:
                        return book.name, await book.prices(engine, handle)
                    except Exception:
                        return None  # one book failing must not cost the others

            by_number: dict[int, dict[str, float]] = {}
            for got in await asyncio.gather(*(price_one(b) for b in self.books)):
                if got is None:
                    continue
                book_name, prices = got
                for runner_norm, p in prices.items():
                    merged.setdefault(runner_norm, {})[book_name] = p["price"]
                    # Saddle number as a second key. TAB truncates long runner names
                    # ("LAST TANGO IN HEAV"), and since the merge matched on the
                    # normalised name alone, every runner TAB had clipped silently
                    # got no book prices at all -- on Globe Derby R2 that was three
                    # of six runners carrying a tote price and nothing else, from
                    # every corporate book at once. The number is the same in all of
                    # them and cannot be truncated.
                    num = p.get("number")
                    if isinstance(num, (int, float)):
                        by_number.setdefault(int(num), {})[book_name] = p["price"]
            if merged:
                self._cache[race.race_key] = merged
                self._cache_by_number[race.race_key] = by_number
                self._last_fetch[race.race_key] = now

        cache = self._cache.get(race.race_key)
        if not cache:
            return
        by_number = self._cache_by_number.get(race.race_key) or {}
        for r in snapshot.runners:
            books = cache.get(norm_runner(r.name))
            if not books and r.number is not None:
                books = by_number.get(int(r.number))
            if not books:
                continue
            r.corp = dict(books)
            best_book, best_price = max(books.items(), key=lambda kv: kv[1])
            r.corp_best = best_price
            r.corp_best_book = best_book

    def coverage(self, races: list[RaceRef]) -> dict[str, int]:
        """How many of `races` each book can actually price — the WS7 metric.

        "The board looks thin" has no symptom of its own, exactly like market-dictionary
        drift: a normalisation change that quietly stops matching shows up as a quiet
        day, not as an error. This is the number that makes it visible.
        """
        return {b.name: sum(1 for r in races if b.handle_for(r) is not None) for b in self.books}

    def prune(self, keep_keys: set[str]) -> None:
        for key in list(self._cache):
            if key not in keep_keys:
                self._cache.pop(key, None)
                self._cache_by_number.pop(key, None)
                self._last_fetch.pop(key, None)
