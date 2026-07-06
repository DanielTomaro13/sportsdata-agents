"""Racing value: one book OUT from the exchange or from the pack, per runner.

Racing cannot join through the resolver the way team sports do — a Betfair
MEETING is one event whose markets are the races, while the books list one
event per race. Races are therefore matched directly: same race number,
start times within a few minutes, and overlapping runner names. Runner
identity is the NAME (the AU books quote saddle numbers as selections but
carry the name in meta; Betfair names carry a "4. " prefix the normalizer
strips).

Fair per runner comes from the exchange's de-vigged win prices when Betfair
covers the race, else from the MEDIAN of the other books' de-vigged prices
(consensus mode, ≥3 OTHER books). A book paying above fair by the threshold
is the signal: ``edge_pct = odds * fair_prob - 1``.

LONGSHOT HONESTY: proportional de-vig overstates longshot probabilities
(the favourite-longshot bias the deferred per-book margin curves will fix
properly), so a 100-1 runner reads as a monster "edge" against a naive
fair. Runners whose fair odds exceed ``max_fair_odds`` are NOT flagged —
the scan only speaks where the de-vig is trustworthy.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import OddsSnapshot

__all__ = ["scan_racing_value"]

_RACING_SPORTS = ("horse_racing", "greyhound_racing", "harness_racing")
_RACE_NO = re.compile(r"\bR(\d+)\b", re.IGNORECASE)


@dataclass
class _RaceUnit:
    book: str
    provider: str
    event_id: str
    event_name: str
    sport: str
    race_no: int
    start: dt.datetime | None
    last_seen: dt.datetime | None = None
    matched: float = 0.0  # exchange only: money traded on the market
    runners: dict[str, float] = field(default_factory=dict)  # name -> odds
    numbers: dict[str, Any] = field(default_factory=dict)  # name -> saddle no

    def devig(self) -> dict[str, float]:
        inv = sum(1.0 / o for o in self.runners.values())
        return {name: (1.0 / odds) / inv for name, odds in self.runners.items()}


def _as_utc(when: dt.datetime | None) -> dt.datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


async def scan_racing_value(
    session: AsyncSession,
    *,
    exchange_book: str = "Betfair",
    hours: float = 0.75,
    min_edge_pct: float = 8.0,
    max_fair_odds: float = 12.0,
    max_edge_pct: float = 60.0,
    max_staleness_minutes: float = 10.0,
    min_matched: float = 500.0,
    exclude_books: tuple[str, ...] = ("FanDuel",),  # not bettable from AU
    min_field_overlap: int = 3,
    limit: int = 20,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now(dt.UTC)
    rows = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.market == "win",
            OddsSnapshot.sport.in_(_RACING_SPORTS),
            OddsSnapshot.captured_at > now - dt.timedelta(hours=hours),
        ).order_by(OddsSnapshot.captured_at)
    )).scalars().all()

    units: dict[tuple[str, str], _RaceUnit] = {}
    for row in rows:
        meta = row.meta or {}
        if row.book == exchange_book:
            race_label = str(meta.get("race", ""))
            match = _RACE_NO.search(race_label)
            if not match:
                continue  # a futures/named market, not a numbered race
            key = (row.book, str(meta.get("market_id", row.event_external_id)))
            race_no = int(match.group(1))
        else:
            match = _RACE_NO.search(row.event_name)
            if not match:
                continue
            key = (row.book, row.event_external_id)
            race_no = int(match.group(1))
        name = str(meta.get("runner") or row.selection).lower().strip()
        if not name or name.isdigit():
            continue  # a bare saddle number cannot match across books
        unit = units.get(key)
        if unit is None:
            unit = units[key] = _RaceUnit(
                book=row.book, provider=row.provider,
                event_id=row.event_external_id,
                event_name=row.event_name, sport=row.sport,
                race_no=race_no, start=_as_utc(row.start_time))
        unit.runners[name] = float(row.odds)  # ascending order: last write wins
        if row.book == exchange_book and meta.get("total_matched") is not None:
            with contextlib.suppress(TypeError, ValueError):
                unit.matched = max(unit.matched, float(meta["total_matched"]))
        seen = row.captured_at if row.captured_at.tzinfo else row.captured_at.replace(tzinfo=dt.UTC)
        unit.last_seen = max(unit.last_seen, seen) if unit.last_seen else seen
        number = meta.get("runner_number")
        if number is None and str(row.selection).isdigit():
            number = int(row.selection)
        if number is not None:
            unit.numbers[name] = number

    # cluster: same race number, starts within 4 minutes, runners overlap
    clusters: list[list[_RaceUnit]] = []
    for unit in sorted(units.values(), key=lambda u: (u.race_no, u.book)):
        placed = False
        for cluster in clusters:
            seed = cluster[0]
            if unit.race_no != seed.race_no:
                continue
            if unit.start and seed.start and abs((unit.start - seed.start).total_seconds()) > 240:
                continue
            if len(set(unit.runners) & set(seed.runners)) < min_field_overlap:
                continue
            cluster.append(unit)
            placed = True
            break
        if not placed:
            clusters.append([unit])

    found: list[dict[str, Any]] = []
    for cluster in clusters:
        # STALENESS: books re-quote on different cadences — a quote much older
        # than the cluster's freshest is a lagging line masquerading as edge
        # (lived: a US book's hour-old AU racing prices read as +171%).
        freshest = max((u.last_seen for u in cluster if u.last_seen), default=None)
        if freshest is not None:
            bound = dt.timedelta(minutes=max_staleness_minutes)
            cluster = [u for u in cluster if u.last_seen and freshest - u.last_seen <= bound]
        books = [u for u in cluster if u.book != exchange_book]
        exchange = next((u for u in cluster if u.book == exchange_book), None)
        # a near-untraded exchange race is not a fair-price source — its backs
        # are stray unmatched offers; fall back to the pack consensus instead
        if exchange is not None and exchange.matched < min_matched:
            exchange = None
        # …and even a traded race can carry ONE junk quote: a stray low back on
        # a longshot inflates that runner's de-vigged prob and every book reads
        # +500% on it (lived: Betfair "fair" 8.6 on a horse the whole pack had
        # at 51-67). A clean win board's back-implied sum sits just under 1;
        # far outside that band means the board is poisoned — use the pack.
        if exchange is not None and exchange.runners:
            inv = sum(1.0 / o for o in exchange.runners.values())
            if not (0.90 <= inv <= 1.15):
                exchange = None
        if not books:
            continue
        # excluded books still CONTRIBUTE to the consensus (more data = a
        # steadier median) but are never flagged — an alert on a book the
        # operator cannot bet is noise
        flaggable = [u for u in books if u.book not in exclude_books]
        if exchange is None and len(books) < 3:
            continue  # consensus needs a pack to be out from
        fairs = {u.book: u.devig() for u in books}
        exchange_fair = exchange.devig() if exchange else None
        for unit in flaggable:
            start = unit.start
            if start is not None and start <= now:
                continue  # the race has jumped — not an offer anyone can take
            for name, odds in unit.runners.items():
                matched: float | None = None
                if exchange_fair is not None:
                    fair = exchange_fair.get(name)
                    versus = exchange_book
                    matched = exchange.matched if exchange else None
                else:
                    others = [f[name] for b, f in fairs.items() if b != unit.book and name in f]
                    if len(others) < 3:
                        continue  # a 2-book "consensus" is one stale quote from a mirage
                    fair = statistics.median(others)
                    versus = f"consensus of {len(others)} books"
                if fair is None or fair <= 0.0:
                    continue
                if 1.0 / fair > max_fair_odds:
                    continue  # longshot territory — proportional de-vig lies out here
                edge_pct = (odds * fair - 1.0) * 100.0
                if edge_pct < min_edge_pct:
                    continue
                # a genuine racing value edge vs the pack is rarely huge; an
                # enormous one is almost always a data artifact (a mis-read
                # price, a scratched/suspended runner the book still lists) —
                # NOT a bet. The ceiling refuses to alert on the implausible.
                if edge_pct > max_edge_pct:
                    continue
                number = unit.numbers.get(name)
                found.append({
                    "sport": unit.sport,
                    "race": unit.event_name,  # e.g. "Pakenham R5" — the book's own label
                    "race_no": unit.race_no,
                    "runner": name.title(),
                    "runner_number": number,
                    "book": unit.book,
                    "odds": odds,
                    "fair_odds": round(1.0 / fair, 2),
                    "versus": versus,
                    "edge_pct": round(edge_pct, 2),
                    "start_time": start.isoformat() if start else None,
                    # settlement keys: the P&L scoreboard grades the alert
                    # against this event's recorded result
                    "provider": unit.provider,
                    "event_external_id": unit.event_id,
                    "exchange_matched": round(matched, 2) if matched is not None else None,
                    # when the flagged book's price was captured — the market
                    # keeps moving after the alert, especially near the jump
                    "seen": unit.last_seen.isoformat() if unit.last_seen else None,
                })
    found.sort(key=lambda c: -c["edge_pct"])
    return found[:limit]
