"""Cross-book arbitrage detection (deterministic, no LLM).

An arb exists on one fixture+market when the best available odds across books
cover a COMPLETE outcome set with ``sum(1/odds) < 1``. The pitfalls this module
exists to avoid:

- **Completeness is per-book.** A single book's own selection set for its market
  is complete by construction; the engine takes the LARGEST per-book outcome set
  as the frame and requires every outcome covered — two books' home+away never
  fake an arb on a market where a third book lists the draw.
- **Orientation.** "home"/"away" are each book's own listing order; sides
  translate into the fixture's frame through the book's published event name
  (the backtest's translation rule). Untranslatable books drop out — never guess.
- **Lines.** Totals only combine on the SAME line; "over 165.5" and "under 166"
  are different markets.
- **Exchange NO contracts.** On a two-outcome frame, Kalshi's "no X" IS the
  other side and folds into it — the classic exchange-vs-book arb surface.
- **Freshness.** A stale price is not an offer: legs price from the SNAPSHOT
  series inside the scan window, so every leg was seen LISTED recently — a book
  that delisted a market keeps its last change-point forever and must not arb.
- **In-play.** Started events are skipped outright: live prices churn faster
  than any capture cadence, so one pre-game leg plus one in-play leg fakes a
  monster margin that no one can take.

Quoted margins are GROSS: exchange fees, book limits and the seconds between
detection and action are the caller's verification problem — every surface that
shows an arb says so.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, Fixture, OddsSnapshot
from sportsdata_agents.operations.resolution.resolver import _side_ok, _tokens, split_sides
from sportsdata_agents.quant.backtest import _translate_side

logger = logging.getLogger(__name__)

DEFAULT_MARKETS = ("h2h", "total")


def _canonical_outcome(selection: str, book_event_name: str, fixture_name: str) -> str | None:
    """A book's selection → an outcome key in the FIXTURE's frame; None = drop
    (untranslatable orientation, unmatched team name, exotic selection)."""
    sel = selection.strip().lower()
    if sel == "draw":
        return "draw"
    if sel in ("home", "away"):
        # translate FROM the book's listing order INTO the fixture's
        return _translate_side(sel, fixture_name, book_event_name)
    first, _, rest = sel.partition(" ")
    if first in ("over", "under") and rest:
        return f"{first} {rest}"  # the line stays in the key — same-line only
    sides = split_sides(fixture_name)
    if not sides:
        return None
    negated = sel.startswith("no ")
    name = sel[3:] if negated else sel
    tokens = _tokens(name)
    if not tokens:
        return None
    is_home = _side_ok(tokens, _tokens(sides[0]))
    is_away = _side_ok(tokens, _tokens(sides[1]))
    if is_home == is_away:  # neither or both — never guess
        return None
    side = "home" if is_home else "away"
    return f"no:{side}" if negated else side


def _board_key(market: str, outcome: str) -> tuple[str, str]:
    """(market, line) — totals split into one board per line."""
    first, _, rest = outcome.partition(" ")
    if first in ("over", "under"):
        return (market, rest)
    return (market, "")


def arbs_for_fixture(
    fixture_name: str,
    market: str,
    rows: list[dict[str, Any]],
    *,
    threshold_pct: float = 1.0,
) -> list[dict[str, Any]]:
    """Arbs on one fixture+market. ``rows`` are the latest price per
    (provider, book, selection): {provider, book, selection, odds, event_name}."""
    # canonicalize, split into per-line boards
    boards: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            if float(row["odds"]) < 1.01:  # junk/placeholder quotes never arb
                continue
        except (TypeError, ValueError):
            continue
        outcome = _canonical_outcome(str(row["selection"]), str(row["event_name"]), fixture_name)
        if outcome is None:
            continue
        entry = dict(row, outcome=outcome)
        plain = outcome[3:] if outcome.startswith("no:") else outcome
        boards.setdefault(_board_key(market, plain), []).append(entry)

    out: list[dict[str, Any]] = []
    for (mkt, line), entries in boards.items():
        # the frame: the largest POSITIVE outcome set any single book lists
        by_book: dict[tuple[str, str], set[str]] = {}
        for e in entries:
            if not e["outcome"].startswith("no:"):
                by_book.setdefault((e["provider"], e["book"]), set()).add(e["outcome"])
        if not by_book:
            continue
        frame = max(by_book.values(), key=len)
        if len(frame) < 2:
            continue
        # best price per outcome; NO contracts fold into the other side on
        # two-outcome frames only (on a 3-way, "not home" is not "away")
        best: dict[str, dict[str, Any]] = {}
        for e in entries:
            outcome = e["outcome"]
            if outcome.startswith("no:"):
                if len(frame) != 2 or "draw" in frame:
                    continue
                side = outcome[3:]
                outcome = next((o for o in frame if o != side), None)
                if outcome is None:
                    continue
            if outcome not in frame:
                continue
            if outcome not in best or float(e["odds"]) > float(best[outcome]["odds"]):
                best[outcome] = e
        if set(best) != frame:
            continue  # an outcome nobody prices — not a complete board
        if len({(e["provider"], e["book"]) for e in best.values()}) < 2:
            continue  # one book "arbing" itself is a capture artifact, not an offer
        inv = sum(1.0 / float(e["odds"]) for e in best.values())
        margin_pct = (1.0 - inv) * 100.0
        if margin_pct < threshold_pct:
            continue
        legs = [
            {
                "outcome": outcome,
                "book": e["book"],
                "provider": e["provider"],
                "odds": round(float(e["odds"]), 3),
                "listed_as": e["selection"],
                # equalised payout: stake share ∝ 1/odds
                "stake_share": round((1.0 / float(e["odds"])) / inv, 4),
            }
            for outcome, e in sorted(best.items())
        ]
        out.append({
            "fixture": fixture_name,
            "market": mkt,
            "line": line,
            "margin_pct": round(margin_pct, 2),
            "sum_inverse": round(inv, 4),
            "legs": legs,
            "note": "gross margin — verify every leg is still live; exchange legs pay "
                    "fees on top; books may limit or void",
        })
    return sorted(out, key=lambda a: -a["margin_pct"])


async def scan_arbs(
    session: AsyncSession,
    *,
    hours: float = 6.0,
    threshold_pct: float = 1.0,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    limit: int = 20,
    max_fixtures: int = 400,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Pre-game arbs across every fixture with ≥2 books re-captured within
    ``hours``.

    Bulk-queried: the warehouse is millions of rows and the arb watch shares a
    5-minute cycle (and a SQLite writer) with everything else — per-fixture
    round-trips measured in the MINUTES; three chunked queries take seconds."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(hours=hours)
    fresh = {
        (provider, external_id)
        for provider, external_id in (
            await session.execute(
                select(OddsSnapshot.provider, OddsSnapshot.event_external_id)
                .where(OddsSnapshot.captured_at >= cutoff)
                .distinct()
            )
        ).all()
    }
    mappings = (
        await session.execute(select(Event).where(Event.fixture_id.is_not(None)))
    ).scalars().all()
    by_fixture: dict[uuid.UUID, list[Event]] = {}
    for m in mappings:
        if m.fixture_id is not None and (m.provider, m.external_id) in fresh:
            by_fixture.setdefault(m.fixture_id, []).append(m)
    candidates = {
        fid: ms
        for fid, ms in by_fixture.items()
        if len({m.provider for m in ms}) >= 2
    }
    if not candidates:
        return []
    fixtures = {
        f.id: f
        for f in (
            await session.execute(select(Fixture).where(Fixture.id.in_(list(candidates))))
        ).scalars().all()
    }
    if len(candidates) > max_fixtures:
        # deterministic cap BEFORE the bulk queries: soonest-starting boards are
        # the actionable ones (an arbitrary dict-order cut also drifts run-to-run)
        def _start_key(fid: uuid.UUID) -> str:
            fx = fixtures.get(fid)
            return fx.start_time.isoformat() if fx is not None and fx.start_time else "9999"

        keep = sorted(candidates, key=_start_key)[:max_fixtures]
        candidates = {fid: candidates[fid] for fid in keep}
    pair_to_fixture = {
        (m.provider, m.external_id): fid for fid, ms in candidates.items() for m in ms
    }
    external_ids = sorted({ext for _p, ext in pair_to_fixture})

    def _chunks(items: list[str], size: int = 500) -> list[list[str]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    # latest LISTED quote per (provider, book, event, selection): snapshots are
    # written every capture (prices only on change), so a row inside the window
    # proves the book still lists the selection — change-points can be stale for
    # delisted markets and would manufacture monster "arbs"
    latest: dict[tuple[str, str, str, str], tuple[str, float]] = {}
    names: dict[tuple[str, str], str] = {}
    for chunk in _chunks(external_ids):
        snap_rows = (
            await session.execute(
                select(
                    OddsSnapshot.provider, OddsSnapshot.book,
                    OddsSnapshot.event_external_id, OddsSnapshot.selection,
                    OddsSnapshot.market, OddsSnapshot.odds, OddsSnapshot.event_name,
                )
                .where(
                    OddsSnapshot.event_external_id.in_(chunk),
                    OddsSnapshot.captured_at >= cutoff,
                    OddsSnapshot.market.in_(markets),
                )
                .order_by(OddsSnapshot.captured_at)
            )
        ).all()
        for provider, book, external_id, selection, market, odds, event_name in snap_rows:
            # ascending — last write per key wins
            if (provider, external_id) in pair_to_fixture:
                latest[(provider, book, external_id, selection)] = (market, float(odds))
                names[(provider, external_id)] = str(event_name or "")

    grouped: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
    for (provider, book, external_id, selection), (market, odds) in latest.items():
        fixture_id = pair_to_fixture[(provider, external_id)]
        grouped.setdefault((fixture_id, market), []).append({
            "provider": provider, "book": book, "selection": selection,
            "odds": odds,
            "event_name": names.get((provider, external_id), ""),
        })

    found: list[dict[str, Any]] = []
    for (fixture_id, market), rows in grouped.items():
        fixture = fixtures.get(fixture_id)
        if fixture is None:
            continue
        start = fixture.start_time
        if start is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=dt.UTC)
            if start <= now:
                continue  # in play or done — not an offer anyone can take
        for arb in arbs_for_fixture(fixture.name, market, rows, threshold_pct=threshold_pct):
            arb["fixture_id"] = str(fixture_id)
            arb["sport"] = fixture.sport
            arb["start_time"] = fixture.start_time.isoformat() if fixture.start_time else None
            found.append(arb)
    found.sort(key=lambda a: -a["margin_pct"])
    return found[:limit]


async def find_arbs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hours: float = 6.0,
    threshold_pct: float = 1.0,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    limit: int = 20,
) -> dict[str, Any]:
    """The session-factory wrapper agents and the CLI call."""
    async with session_factory() as session:
        arbs = await scan_arbs(
            session, hours=hours, threshold_pct=threshold_pct, markets=markets, limit=limit
        )
    note = "pre-game only; margins are GROSS — exchange fees, limits and timing are not priced in"
    return {
        "arbs": arbs,
        "scanned_markets": list(markets),
        "freshness_hours": hours,
        "threshold_pct": threshold_pct,
        "note": note,
    }
