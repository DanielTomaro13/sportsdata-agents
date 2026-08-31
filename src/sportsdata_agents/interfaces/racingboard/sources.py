"""
Data sources: discover races and snapshot per-race money flow.

Spine = TAB meetings (cleanly enumerates R/G/H with venue, race number, start
time and runner names). For each race:
  * TAB tote     -> pari-mutuel pool share per runner (the money signal, all codes)
  * TAB fixed    -> fixed-odds win price
  * Betfair      -> weight of money + matched + price (horses/greys only)

Cross-book matching is best-effort: normalise the venue name, match on race
number, then match runners by normalised name.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .betfair import BetfairClient, HORSE_RACING, GREYHOUND_RACING
from .config import settings
from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot, RunnerFlow


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# Venue and runner matching live in `venues.py` — the exact-then-unique-subset rule
# that lets `MOHAWK` reach `Woodbine Mohawk Park` without letting `Woodbine` do the
# same. Re-exported here because the Betfair matcher below joins on the same keys.
from .venues import norm_runner as _norm_runner  # noqa: E402
from .venues import norm_venue as _norm_venue  # noqa: E402
from .venues import unique_venue_match  # noqa: E402


def _to_epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# TAB tote source
# --------------------------------------------------------------------------

async def tab_snapshot(engine: SportsDataEngine, race: RaceRef) -> RaceSnapshot | None:
    data = await engine.try_call(
        "tab_racing_race",
        date=race.date,
        raceType=race.code,
        venueMnemonic=race.venue_mnem,
        raceNumber=race.race_no,
        jurisdiction=settings.jurisdiction,
    )
    if not data:
        return None

    runners: list[RunnerFlow] = []
    implied: dict[int, float] = {}
    for r in data.get("runners", []):
        num = r.get("runnerNumber")
        if num is None:
            continue
        scratched = bool(r.get("scratched")) or (r.get("runnerStatus") == "SCRATCHED")
        pari = (r.get("parimutuel") or {}).get("returnWin")
        fixed = (r.get("fixedOdds") or {}).get("returnWin")
        rf = RunnerFlow(
            number=int(num),
            name=r.get("runnerName", ""),
            scratched=scratched,
            tote_win=pari if pari and pari > 0 else None,
            fixed_win=fixed if fixed and fixed > 0 else None,
        )
        if rf.tote_win and not scratched:
            implied[rf.number] = 1.0 / rf.tote_win
        runners.append(rf)

    # Normalise tote implied prob into pool share.
    total = sum(implied.values())
    if total > 0:
        for rf in runners:
            if rf.number in implied:
                rf.tote_pool_share = implied[rf.number] / total

    # Race-level win pool. TAB reports it as `poolTotal` on the Win product; it
    # populates well before the jump (not `grossPoolAmount`, which stays null).
    win_pool = None
    for p in data.get("pools") or []:
        if p.get("wageringProduct") == "Win" and p.get("poolTotal"):
            win_pool = p.get("poolTotal")
            break

    status = "OPEN"
    if data.get("results"):
        status = "RESULTED"
    elif str(data.get("raceStatus", "")).upper() in ("CLOSED", "INTERIM", "PAYING"):
        status = data["raceStatus"].upper()

    return RaceSnapshot(
        ts=time.time(),
        runners=runners,
        tote_win_pool=win_pool,
        status=status,
    )


# --------------------------------------------------------------------------
# Betfair enrichment (horses + greyhounds)
# --------------------------------------------------------------------------

class BetfairMatcher:
    """Builds and caches a {(code, venue_norm, race_no): market_id} index."""

    def __init__(self, client: BetfairClient) -> None:
        self.client = client
        self._market_index: dict[tuple[str, str, int], str] = {}
        self._meeting_scanned: set[str] = set()

    async def _top_meetings(self, event_type: str) -> list[dict[str, Any]]:
        graph = await self.client.navigation(
            event_type, attachments="MENU,EVENT", max_out_distance=2, max_results=500
        )
        return [
            n for n in graph.get("nodes", [])
            if n.get("nodeType") == "MENU" and (n.get("navInfo") or {}).get("isMeetingNode")
        ]

    async def refresh_for(self, races: list[RaceRef]) -> None:
        """Ensure WIN market ids are indexed for the venues in `races`."""
        wanted_codes = {r.code for r in races if r.code in ("R", "G")}
        if not wanted_codes:
            return
        wanted_venues = {_norm_venue(r.venue) for r in races if r.code in ("R", "G")}

        code_event = {"R": HORSE_RACING, "G": GREYHOUND_RACING}
        for code in wanted_codes:
            try:
                meetings = await self._top_meetings(code_event[code])
            except Exception:
                continue
            for mn in meetings:
                vnorm = _norm_venue(mn.get("name", ""))
                if unique_venue_match(mn.get("name", ""),
                                      [(v, v) for v in wanted_venues]) is None:
                    continue
                menu_id = mn["nodeId"]
                if menu_id in self._meeting_scanned:
                    continue
                await self._scan_meeting(code, vnorm, menu_id)
                self._meeting_scanned.add(menu_id)

    async def _scan_meeting(self, code: str, vnorm: str, menu_id: str) -> None:
        try:
            graph = await self.client.navigation(
                menu_id, attachments="MENU,EVENT,MARKET",
                max_out_distance=3, max_results=1000,
            )
        except Exception:
            return
        for n in graph.get("nodes", []):
            if n.get("nodeType") != "MARKET":
                continue
            info = n.get("marketInfo") or {}
            if info.get("marketType", "").upper() != "WIN":
                continue
            # Horses carry raceNumber ("R6"); greyhounds leave it null and only
            # put the number in the market name ("R6 366m Heat"). Try both.
            src = (info.get("raceNumber") or "") + " " + (info.get("marketName") or "")
            m = re.search(r"\d+", src)
            if not m:
                continue
            race_no = int(m.group())
            self._market_index[(code, vnorm, race_no)] = info["marketId"]

    def market_id_for(self, race: RaceRef) -> str | None:
        exact = self._market_index.get((race.code, race.venue_key, race.race_no))
        if exact:
            return exact
        # Otherwise resolve the name properly: same code and race number, and the one
        # venue that uniquely names this track. Refuses when two could, because a wrong
        # market id prices the wrong race.
        candidates = [(vnorm, mid) for (code, vnorm, no), mid in self._market_index.items()
                      if code == race.code and no == race.race_no]
        return unique_venue_match(race.venue, candidates)


def _best(levels: list[dict[str, Any]] | None) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    top = levels[0]
    return top.get("price"), top.get("size")


async def betfair_enrich(
    client: BetfairClient, market_id: str, snapshot: RaceSnapshot
) -> None:
    """Overlay Betfair WoM / matched / price onto a snapshot's runners (in place)."""
    blocks = await client.market_prices([market_id])
    for et in blocks:
        for ev in et.get("eventNodes", []):
            for mkt in ev.get("marketNodes", []):
                apply_betfair_market(snapshot, mkt)


def apply_betfair_market(snapshot: RaceSnapshot, mkt: dict) -> None:
    """Overlay ONE exchange market node onto a snapshot's runners, in place.

    Split out of betfair_enrich so the fast loop can batch every active market
    into a single market_prices call and then apply the answers, instead of one
    request per race. Betfair is the sharpest number the board has and the base of
    the fair price, so how often it lands is the board's real clock.
    """
    by_name = {_norm_runner(r.name): r for r in snapshot.runners}
    state = mkt.get("state", {}) or {}
    snapshot.bf_total_matched = state.get("totalMatched")
    for run in mkt.get("runners", []):
        name = (run.get("description", {}) or {}).get("runnerName", "")
        rf = by_name.get(_norm_runner(name))
        if rf is None:
            continue
        exch = run.get("exchange", {}) or {}
        back_p, back_s = _best(exch.get("availableToBack"))
        lay_p, lay_s = _best(exch.get("availableToLay"))
        rf.bf_back = back_p
        rf.bf_lay = lay_p
        rf.bf_last = (run.get("state", {}) or {}).get("lastPriceTraded")
        if back_s and lay_s:
            rf.bf_wom = back_s / (back_s + lay_s)
        if back_p and lay_p:
            mid = (back_p + lay_p) / 2
            rf.bf_implied = 1.0 / mid if mid else None


def finalize_snapshot(snapshot: RaceSnapshot) -> None:
    """Compute a fair price per runner and the value edge of the best book price.

    Fair probabilities come from the sharpest market available: proportional
    de-vig of Betfair mid prices when the race has real exchange coverage, else
    the tote pool share (pari-mutuel is already overround-free). Method borrowed
    from sportsdata-agents' quant/devig: fair_prob = (1/odds) / Σ(1/odds).
    Value edge = (best_book_price × fair_prob − 1) × 100, guarded against the
    longshot/data-artifact regime.
    """
    active = [r for r in snapshot.runners if not r.scratched]
    if len(active) < 2:
        return

    fair_prob: dict[int, float] = {}
    source: dict[int, str] = {}
    threshold = max(4, int(0.6 * len(active)))

    # The sportsdata racing engine wins when it covers the field: the form
    # model sees a wide barrier or a 3kg swing the market prices slowly.
    # Renormalise its probs over the runners it covers so fair is a proper
    # distribution even when a couple of runners are engine-blind.
    eng = {r.number: r.engine_prob for r in active
           if r.engine_prob and r.engine_prob > 0}
    if len(eng) >= threshold:
        total = sum(eng.values())
        if total > 0:
            for num, p in eng.items():
                fair_prob[num] = p / total
                source[num] = "engine"

    # Betfair mids fill anyone the engine didn't cover (and engine-less races).
    mids = {r.number: (r.bf_back + r.bf_lay) / 2
            for r in active if r.bf_back and r.bf_lay}
    if len(mids) >= threshold:
        inv_total = sum(1.0 / m for m in mids.values())
        if inv_total > 0:
            for num, m in mids.items():
                if num not in fair_prob:
                    fair_prob[num] = (1.0 / m) / inv_total
                    source[num] = "betfair"

    # Tote pool share fills any runner still uncovered (and tote-only races).
    for r in active:
        if r.number not in fair_prob and r.tote_pool_share:
            fair_prob[r.number] = r.tote_pool_share
            source[r.number] = "tote"

    for r in active:
        fp = fair_prob.get(r.number)
        if not fp or fp <= 0:
            continue
        r.fair_price = round(1.0 / fp, 2)
        r.fair_source = source.get(r.number)
        # Value only where the fair estimate is trustworthy (not deep longshots)
        # and the edge is plausible (a huge one is a stale/scratched-price mirage).
        if r.corp_best and r.fair_price <= 20:
            edge = (r.corp_best * fp - 1.0) * 100.0
            if -60 < edge < 60:
                r.value_pct = round(edge, 1)
