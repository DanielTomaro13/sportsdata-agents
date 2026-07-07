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

# books the operator cannot BET from AU — they may inform fairs/boards but a
# recommendation to back/leg them is noise (racing_value's rule, generalised):
# Kalshi is US-persons-only, Polymarket has no AU fixed-odds surface, FanDuel
# and Pinnacle don't take AU customers
UNBETTABLE_BOOKS = ("FanDuel", "Kalshi", "Polymarket", "Pinnacle")


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
                # exchange legs carry the market's traded volume; books have none
                **({"matched": round(float(e["matched"]), 2)}
                   if e.get("matched") is not None else {}),
                **({"seen": e["seen"].isoformat()} if e.get("seen") is not None else {}),
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


async def collect_fixture_boards(
    session: AsyncSession,
    *,
    hours: float = 6.0,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    max_fixtures: int = 400,
    now: dt.datetime | None = None,
) -> tuple[dict[uuid.UUID, Fixture], dict[tuple[uuid.UUID, str], list[dict[str, Any]]]]:
    """The cross-book board per (fixture, market): every book's latest LISTED
    quote joined through the resolver's event->fixture mapping.

    Bulk-queried: the warehouse is millions of rows and the watches share a
    5-minute cycle (and a SQLite writer) with everything else — per-fixture
    round-trips measured in the MINUTES; three chunked queries take seconds.
    Shared by the arb scan (sum of best inverses) and the exchange premium
    scan (book price vs de-vigged exchange fair) — one collection, two maths."""
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
        return {}, {}
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
    latest: dict[tuple[str, str, str, str],
                 tuple[str, float, float | None, dt.datetime, float | None]] = {}
    names: dict[tuple[str, str], str] = {}
    for chunk in _chunks(external_ids):
        snap_rows = (
            await session.execute(
                select(
                    OddsSnapshot.provider, OddsSnapshot.book,
                    OddsSnapshot.event_external_id, OddsSnapshot.selection,
                    OddsSnapshot.market, OddsSnapshot.odds, OddsSnapshot.event_name,
                    OddsSnapshot.meta, OddsSnapshot.captured_at,
                )
                .where(
                    OddsSnapshot.event_external_id.in_(chunk),
                    OddsSnapshot.captured_at >= cutoff,
                    OddsSnapshot.market.in_(markets),
                )
                .order_by(OddsSnapshot.captured_at)
            )
        ).all()
        for provider, book, external_id, selection, market, odds, event_name, meta, seen in snap_rows:
            # ascending — last write per key wins
            if (provider, external_id) in pair_to_fixture:
                # exchange-style liquidity: Betfair's traded volume, or a
                # prediction platform's market volume — both answer "has real
                # money been through this market?" Bookmaker rows have neither.
                matched = None
                lay = None
                if isinstance(meta, dict):
                    raw = meta.get("total_matched")
                    if raw is None:
                        raw = meta.get("volume_24h")
                    if raw is not None:
                        try:
                            matched = float(raw)
                        except (TypeError, ValueError):
                            matched = None
                    try:
                        lay = float(meta["lay"]) if meta.get("lay") else None
                    except (TypeError, ValueError):
                        lay = None
                seen_utc = seen if seen.tzinfo else seen.replace(tzinfo=dt.UTC)
                latest[(provider, book, external_id, selection)] = (
                    market, float(odds), matched, seen_utc, lay)
                names[(provider, external_id)] = str(event_name or "")

    grouped: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
    for (provider, book, external_id, selection), (market, odds, matched, seen, lay) in latest.items():
        fixture_id = pair_to_fixture[(provider, external_id)]
        grouped.setdefault((fixture_id, market), []).append({
            "provider": provider, "book": book, "selection": selection,
            "odds": odds, "matched": matched, "seen": seen, "lay": lay,
            "event_name": names.get((provider, external_id), ""),
        })
    return fixtures, grouped


def _pre_game(fixture: Fixture | None, now: dt.datetime) -> bool:
    """Unknown start → we CANNOT confirm the game is pre-game, so a signal here
    could be a stale leg on a live/finished game. Treat unknown as unsafe and
    skip — a clean board is the honest norm, never a fabricated edge live."""
    if fixture is None or fixture.start_time is None:
        return False
    start = fixture.start_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    return start > now


async def scan_arbs(
    session: AsyncSession,
    *,
    hours: float = 6.0,
    threshold_pct: float = 1.0,
    min_matched: float = 1000.0,
    max_age_minutes: float = 20.0,
    min_lead_minutes: float = 15.0,
    exclude_books: tuple[str, ...] = UNBETTABLE_BOOKS,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    limit: int = 20,
    max_fixtures: int = 400,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Pre-game arbs across every fixture with ≥2 books re-captured within
    ``hours`` (collection shared with the exchange premium scan). Books in
    ``exclude_books`` never form a LEG — an arb the operator cannot take is
    noise (lived: back-at-Kalshi recommendations)."""
    from sportsdata_agents.operations.ingestion.coverage import fixture_covered

    now = now or dt.datetime.now(dt.UTC)
    fixtures, grouped = await collect_fixture_boards(
        session, hours=hours, markets=markets, max_fixtures=max_fixtures, now=now)
    found: list[dict[str, Any]] = []
    for (fixture_id, market), rows in grouped.items():
        fixture = fixtures.get(fixture_id)
        if fixture is None or not _pre_game(fixture, now):
            continue  # in play, done, or unconfirmable — not an offer anyone can take
        if not fixture_covered(fixture.sport, fixture.name):
            continue  # tennis doubles etc. — the operator can't/won't bet these
        if fixture.start_time is not None:
            _fx_start = (fixture.start_time if fixture.start_time.tzinfo
                         else fixture.start_time.replace(tzinfo=dt.UTC))
            if _fx_start < now + dt.timedelta(minutes=min_lead_minutes):
                continue  # listed starts are estimates (tennis begins early);
                # a leg going in-play mid-scan fakes the margin
        # a quote from a near-untraded exchange/prediction market is not a
        # takeable leg — it is one stray unmatched offer. Bookmaker rows carry
        # no liquidity figure (their quotes ARE firm offers) and pass through;
        # a Betfair row whose totalMatched the API omitted also passes (fail-
        # open, deliberately: the API sends it in practice)
        rows = [r for r in rows
                if r.get("matched") is None or float(r["matched"]) >= min_matched]
        rows = [r for r in rows if r["book"] not in exclude_books]
        # FRESHNESS: an arb is only real if every leg's price was seen just
        # now — a leg captured half an hour ago on a fast market (tennis!) is
        # the market's PAST, and alerting it reads prices that no longer exist
        age_bound = dt.timedelta(minutes=max_age_minutes)
        rows = [r for r in rows
                if r.get("seen") is None or now - r["seen"] <= age_bound]
        for arb in arbs_for_fixture(fixture.name, market, rows, threshold_pct=threshold_pct):
            arb["fixture_id"] = str(fixture_id)
            arb["sport"] = fixture.sport
            arb["start_time"] = fixture.start_time.isoformat() if fixture.start_time else None
            found.append(arb)
    found.sort(key=lambda a: -a["margin_pct"])
    return found[:limit]


async def scan_exchange_premium(
    session: AsyncSession,
    *,
    exchange_book: str = "Betfair",
    hours: float = 6.0,
    min_edge_pct: float = 3.0,
    min_matched: float = 1000.0,
    require_matched: bool = True,
    max_age_minutes: float = 20.0,
    max_fair_odds: float = 10.0,
    min_lead_minutes: float = 15.0,
    exclude_books: tuple[str, ...] = UNBETTABLE_BOOKS,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    limit: int = 20,
    max_fixtures: int = 400,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Book price vs the DE-VIGGED sharp fair on the same fixture — the
    model-free value signal. The exchange's back prices carry only the
    back/lay spread as overround; a proportional de-vig across the market's
    outcomes yields the fair probabilities, and any book paying more than
    fair is a premium: ``edge_pct = book_odds * fair_prob - 1``.

    ``exchange_book`` names the fair source. A SHARP BOOKMAKER (Pinnacle)
    works too — its boards carry a small margin the proportional de-vig
    removes; set ``require_matched=false`` since books have no matched-money
    concept (the liquidity gate is an exchange-only idea).

    Honest caveats ride every candidate: the exchange charges commission on
    winnings (2-8 pct of PROFIT, not turnover — an edge inside commission is
    still real against the book), and thin exchange markets make a noisy
    fair (min_liability guards via the outcome count only; sizing is the
    staking module's job)."""
    now = now or dt.datetime.now(dt.UTC)
    fixtures, grouped = await collect_fixture_boards(
        session, hours=hours, markets=markets, max_fixtures=max_fixtures, now=now)
    found: list[dict[str, Any]] = []
    from sportsdata_agents.operations.ingestion.coverage import fixture_covered

    for (fixture_id, market), rows in grouped.items():
        fixture = fixtures.get(fixture_id)
        if fixture is None or not _pre_game(fixture, now):
            continue
        if not fixture_covered(fixture.sport, fixture.name):
            continue  # tennis doubles etc. — outside the operator's coverage
        # BUCKET BY LINE: totals list several lines (over/under 165.5, 220.5, …)
        # under one (fixture, "total") group. De-vigging them together sums
        # inverses across lines (inv≈2.0 for two lines) and mangles every edge —
        # each line is its own two-way market and must be de-vigged alone.
        if fixture.start_time is not None:
            _fx_start = (fixture.start_time if fixture.start_time.tzinfo
                         else fixture.start_time.replace(tzinfo=dt.UTC))
            if _fx_start < now + dt.timedelta(minutes=min_lead_minutes):
                continue  # listed starts are estimates (tennis begins early);
                # near/inside the start the fair source freezes while
                # exchanges go live, and live-vs-stale reads as huge edge
        exchange: dict[str, dict[str, float]] = {}  # line -> {outcome: back odds}
        fair_seen: dict[str, Any] = {}  # line -> newest sighting of the fair board
        matched_by_line: dict[str, float] = {}  # line -> money matched on the market
        others: list[dict[str, Any]] = []
        for row in rows:
            outcome = _canonical_outcome(str(row["selection"]), str(row["event_name"]),
                                         fixture.name)
            if outcome is None:
                continue
            _, line = _board_key(market, outcome)
            if row["book"] == exchange_book:
                bucket = exchange.setdefault(line, {})
                # latest listed wins (rows arrive newest-last per collect order)
                bucket[outcome] = float(row["odds"])
                seen = row.get("seen")
                if seen is not None:
                    aware = seen if seen.tzinfo else seen.replace(tzinfo=dt.UTC)
                    prev = fair_seen.get(line)
                    fair_seen[line] = aware if prev is None or aware > prev else prev
                if row.get("matched") is not None:
                    matched_by_line[line] = max(matched_by_line.get(line, 0.0),
                                                float(row["matched"]))
            else:
                if row["book"] in exclude_books:
                    continue  # can't bet it from AU — never flag it
                others.append({**row, "outcome": outcome, "line": line})
        age_bound = dt.timedelta(minutes=max_age_minutes)
        for row in others:
            seen = row.get("seen")
            if seen is not None:
                seen_dt = seen if seen.tzinfo else seen.replace(tzinfo=dt.UTC)
                if now - seen_dt > age_bound:
                    continue  # an hours-old book quote against the CURRENT
                    # exchange fair is a lagging line masquerading as edge
            board = exchange.get(row["line"])
            if not board or len(board) < 2:
                continue  # no de-viggable exchange market at this line
            # the FAIR SOURCE must be fresh too: Pinnacle freezes when a match
            # goes in-play while exchanges keep trading, and a live 5.80
            # against a stale 4.13 'fair' pushed as +40% (lived: 2026-07-07)
            f_seen = fair_seen.get(row["line"])
            if f_seen is None or now - f_seen > age_bound:
                continue
            # LIQUIDITY: an exchange market nobody has traded is not a market —
            # its "backs" are junk offers nobody took (live case: $16 matched on
            # an obscure basketball game read as a +298% "premium"). Fair prices
            # only come from markets with real money through them.
            matched = matched_by_line.get(row["line"], 0.0)
            if require_matched and matched < min_matched:
                continue
            inv = sum(1.0 / o for o in board.values())
            # exchange BACKS sit above fair by the spread, so their implied sum
            # is naturally a little under 1 (a tight two-way ~0.97). Far BELOW
            # means a stale/suspended side manufacturing fair odds; far ABOVE
            # means wide junk offers with no taker — neither is a priceable
            # market (the same $16 game had backs summing to 1.74).
            if inv < 0.90 or inv > 1.08:
                continue
            back = board.get(row["outcome"])
            if back is None:
                continue
            prob = (1.0 / back) / inv
            if prob > 0 and 1.0 / prob > max_fair_odds:
                continue  # longshot territory: proportional de-vig lies out
                # here and a 15.0-vs-"fair"-12.9 gap is spread noise, not edge
            edge_pct = (float(row["odds"]) * prob - 1.0) * 100.0
            if edge_pct < min_edge_pct:
                continue
            found.append({
                "fixture_id": str(fixture_id),
                "fixture": fixture.name,
                "sport": fixture.sport,
                "market": market,
                "outcome": row["outcome"],
                "book": row["book"],
                "odds": float(row["odds"]),
                "exchange_fair_odds": round(1.0 / prob, 3),
                "exchange_back": back,
                "exchange_matched": round(matched, 2),
                "edge_pct": round(edge_pct, 2),
                "seen": row["seen"].isoformat() if row.get("seen") is not None else None,
                "start_time": fixture.start_time.isoformat() if fixture.start_time else None,
                "note": "edge vs de-vigged exchange back prices; exchange commission "
                        "applies to exchange bets only, not this book bet",
            })
    found.sort(key=lambda c: -c["edge_pct"])
    return found[:limit]


async def scan_back_lay(
    session: AsyncSession,
    *,
    exchange_book: str = "Betfair",
    hours: float = 1.0,
    min_margin_pct: float = 1.0,
    min_matched: float = 1000.0,
    commission_pct: float = 5.0,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    limit: int = 20,
    max_fixtures: int = 400,
    max_leg_gap_minutes: float = 10.0,
    min_lead_minutes: float = 20.0,
    exclude_books: tuple[str, ...] = UNBETTABLE_BOOKS,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Back at a book, LAY the same outcome on the exchange — a risk-free
    margin whenever the book's back price clears the exchange's lay price
    (a different surface than the cross-book arb: one outcome, two sides).

    Math per $1 book stake at back odds B, exchange lay odds L, commission c
    on exchange WINNINGS: lay stake = B/L equalises both outcomes;
      outcome wins:  +(B-1) - (B/L)(L-1)      = B(1 - 1/L) - 1 + B/L ... net B/L·(L-... )
    simplified guaranteed profit ≈ B/L - 1 minus commission drag on the lay-
    win branch; we report the CONSERVATIVE branch (commission applied):
      profit_pct = min(B - (B/L)(L-1) - 1, (B/L)(1-c) - 1) x 100
    Only fires when even the worse branch is positive.

    Guard rails (lived 2026-07-07): a tennis doubles match started EARLY
    (listed starts are estimates), the exchange went in-play while the
    books' backs sat 45 minutes stale, and four 20-26% "locked profits"
    pushed. Both legs must now be seen within ``max_leg_gap_minutes`` of
    each other, the listed start must be ``min_lead_minutes`` out, and
    fixtures outside the operator's coverage (tennis doubles) never alert."""
    from sportsdata_agents.operations.ingestion.coverage import fixture_covered

    now = now or dt.datetime.now(dt.UTC)
    fixtures, grouped = await collect_fixture_boards(
        session, hours=hours, markets=markets, max_fixtures=max_fixtures, now=now)
    c = commission_pct / 100.0
    lead = dt.timedelta(minutes=min_lead_minutes)
    leg_gap = dt.timedelta(minutes=max_leg_gap_minutes)
    found: list[dict[str, Any]] = []
    for (fixture_id, market), rows in grouped.items():
        fixture = fixtures.get(fixture_id)
        if fixture is None or not _pre_game(fixture, now):
            continue
        if fixture.start_time is not None:
            start = (fixture.start_time if fixture.start_time.tzinfo
                     else fixture.start_time.replace(tzinfo=dt.UTC))
            if start < now + lead:
                continue  # listed starts are estimates; too close is as bad as live
        if not fixture_covered(fixture.sport, fixture.name):
            continue  # tennis doubles etc. — the operator can't/won't bet these
        lays: dict[str, tuple[float, float, Any]] = {}  # outcome -> (lay, matched, seen)
        backs: list[dict[str, Any]] = []
        for row in rows:
            outcome = _canonical_outcome(str(row["selection"]), str(row["event_name"]),
                                         fixture.name)
            if outcome is None:
                continue
            if row["book"] == exchange_book:
                if row.get("lay") and (row.get("matched") or 0.0) >= min_matched:
                    lays[outcome] = (float(row["lay"]),
                                     float(row.get("matched") or 0.0),
                                     row.get("seen"))
            elif row["book"] not in exclude_books:
                backs.append({**row, "outcome": outcome})
        for row in backs:
            hit = lays.get(row["outcome"])
            if hit is None:
                continue
            lay_odds, matched, lay_seen = hit
            back = float(row["odds"])
            if lay_odds < 1.01 or back <= lay_odds:
                continue  # no gap — the normal state of an efficient market
            back_seen = row.get("seen")
            if back_seen is None or lay_seen is None:
                continue  # unverifiable freshness is stale by assumption
            back_aware = back_seen if back_seen.tzinfo else back_seen.replace(tzinfo=dt.UTC)
            lay_aware = lay_seen if lay_seen.tzinfo else lay_seen.replace(tzinfo=dt.UTC)
            if abs(back_aware - lay_aware) > leg_gap:
                continue  # one stale leg manufactures the whole "margin"
            lay_stake = back / lay_odds
            win_branch = (back - 1.0) - lay_stake * (lay_odds - 1.0)
            lose_branch = lay_stake * (1.0 - c) - 1.0
            profit_pct = min(win_branch, lose_branch) * 100.0
            if profit_pct < min_margin_pct:
                continue
            found.append({
                "fixture_id": str(fixture_id),
                "fixture": fixture.name,
                "sport": fixture.sport,
                "market": market,
                "outcome": row["outcome"],
                "book": row["book"],
                "back_odds": back,
                "lay_odds": lay_odds,
                "lay_stake_per_dollar": round(lay_stake, 3),
                "exchange_matched": round(matched, 2),
                "profit_pct": round(profit_pct, 2),
                "seen": row["seen"].isoformat() if row.get("seen") is not None else None,
                "start_time": fixture.start_time.isoformat() if fixture.start_time else None,
                "note": f"guaranteed margin net of {commission_pct:.0f}% exchange "
                        "commission — verify both prices are still live",
            })
    found.sort(key=lambda x: -x["profit_pct"])
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


async def arb_margin_now(
    session: AsyncSession,
    *,
    fixture_id: str,
    market: str,
    line: str = "",
    hours: float = 1.0,
    now: dt.datetime | None = None,
) -> float | None:
    """The CURRENT gross margin of one (fixture, market, line) board — the alert
    outcome-tracking probe. None when the board is no longer complete (a leg
    delisted, the event started and dropped off the snapshot window): the
    opportunity is gone."""
    fid = uuid.UUID(fixture_id)
    fixture = await session.get(Fixture, fid)
    if fixture is None:
        return None
    mappings = (
        await session.execute(select(Event).where(Event.fixture_id == fid))
    ).scalars().all()
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    latest: dict[tuple[str, str, str], tuple[float, str]] = {}
    for m in mappings:
        snap_rows = (
            await session.execute(
                select(OddsSnapshot.book, OddsSnapshot.selection,
                       OddsSnapshot.odds, OddsSnapshot.event_name)
                .where(
                    OddsSnapshot.provider == m.provider,
                    OddsSnapshot.event_external_id == m.external_id,
                    OddsSnapshot.market == market,
                    OddsSnapshot.captured_at >= cutoff,
                )
                .order_by(OddsSnapshot.captured_at)
            )
        ).all()
        for book, selection, odds, event_name in snap_rows:  # last write wins
            latest[(m.provider, book, selection)] = (float(odds), str(event_name or ""))
    for (provider, book, selection), (odds, event_name) in latest.items():
        rows.append({"provider": provider, "book": book, "selection": selection,
                     "odds": odds, "event_name": event_name})
    # threshold below any real margin: we want the CURRENT margin even when the
    # board has decayed negative
    for arb in arbs_for_fixture(fixture.name, market, rows, threshold_pct=-1000.0):
        if arb["line"] == line:
            return float(arb["margin_pct"])
    return None
