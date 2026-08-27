"""Warehouse reader for the sports board.

The ingest already fills the warehouse with every book, the exchange and the
prediction markets, all resolved onto shared fixtures. This reads that: per
game it assembles the cross-source quotes for EVERY core market (head-to-head,
totals and spreads, at every line the sharps priced), blends the sharp line,
values the books, and adds Betfair money + the engine rating — no new scrapers,
it rides the pipeline that's already there. Every league is included; only
racing is held out (it has its own board).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import (
    Event,
    Fixture,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
)
from sportsdata_agents.quant.sharp_line import SHARP_SOURCES, sharp_line

# racing has its own board (tote pool flow); everything else is a "game"
RACING_SPORTS = frozenset({"horse_racing", "greyhound_racing", "harness_racing",
                           "racing", "horse", "greyhound", "harness"})
_SIDES = ("home", "away", "draw")
_MAX_MARKETS = 40  # cap the alt-line explosion on a busy game


def _market_key(family: str, line: float | None) -> str:
    return family if line is None else f"{family} {line:g}"


def _market_label(family: str, line: float | None) -> str:
    if family == "h2h":
        return "Head to Head"
    if family == "total":
        return f"Total O/U {line:g}" if line is not None else "Total"
    if family == "line":
        return f"Line {line:+g}" if line is not None else "Line"
    return family


async def _fixture_events(session: AsyncSession, fixture_ids: set[Any]) -> dict[Any, list[Event]]:
    if not fixture_ids:
        return {}
    rows = (await session.execute(
        select(Event).where(Event.fixture_id.in_(fixture_ids)))).scalars().all()
    out: dict[Any, list[Event]] = {}
    for e in rows:
        out.setdefault(e.fixture_id, []).append(e)
    return out


async def _markets_by_source(
    session: AsyncSession, events: list[Event], *,
    now: dt.datetime | None = None, fresh_minutes: float = 20.0,
) -> tuple[dict[tuple[str, float | None], dict[str, dict[str, float]]], dict[str, Any],
           dict[str, dict[str, dict[str, float]]]]:
    """{(family, line): {source: {side: odds}}} across h2h/total/line for the
    freshest snapshot per (source, market, side), plus Betfair money, plus the
    EXTRAS: every other market the books price ({market: {selection: {book:
    odds}}}) — player props, disposals, novelty derivatives. No sharp blends
    over these (no two sides to de-vig), but cross-book comparison is exactly
    what a punter wants from them."""

    if not events:
        return {}, {}, {}
    ext_ids = {e.external_id for e in events}
    now = now or dt.datetime.now(dt.UTC)
    floor = now - dt.timedelta(minutes=fresh_minutes)
    snaps = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.event_external_id.in_(ext_ids),
            OddsSnapshot.captured_at >= floor,
            OddsSnapshot.captured_at <= now,  # freshest snapshot AS OF `now` (no-op live; rewinds for replay capture)
        ).order_by(OddsSnapshot.captured_at.desc()))).scalars().all()
    return _classify_snaps(snaps)


def _classify_snaps(snaps: Sequence[OddsSnapshot]) -> tuple[
        dict[tuple[str, float | None], dict[str, dict[str, float]]], dict[str, Any],
        dict[str, dict[str, dict[str, float]]]]:
    """Pure grouping of freshest-first snapshot rows — split from the fetch so
    list_games can classify hundreds of fixtures from ONE bulk query instead
    of one query each (500 fixtures was 500 round-trips per board poll)."""
    from sportsdata_agents.operations.monitoring import _market_family, _split_selection

    markets: dict[tuple[str, float | None], dict[str, dict[str, float]]] = {}
    seen: set[tuple[str, str, float | None, str]] = set()
    money: dict[str, Any] = {}
    extras: dict[str, dict[str, dict[str, float]]] = {}
    extras_seen: set[tuple[str, str, str]] = set()
    for s in snaps:
        family = _market_family(s.market)
        if family not in ("h2h", "total", "line"):
            ekey = (s.book, s.market, str(s.selection))
            if ekey in extras_seen:
                continue
            extras_seen.add(ekey)
            try:
                eodds = float(s.odds)
            except (TypeError, ValueError):
                continue
            if eodds > 1.0:
                extras.setdefault(s.market, {}).setdefault(str(s.selection), {})[s.book] = eodds
            continue
        side, line = _split_selection(str(s.selection).lower())
        if family == "h2h" and (line is not None or side not in _SIDES):
            continue
        if family in ("total", "line") and (line is None or side not in
                                            ("over", "under", "home", "away")):
            continue
        key = (s.book, family, line, side)
        if key in seen:  # newest wins (rows are captured_at desc)
            continue
        seen.add(key)
        try:
            odds = float(s.odds)
        except (TypeError, ValueError):
            continue
        if odds > 1.0:
            markets.setdefault((family, line), {}).setdefault(s.book, {})[side] = odds
        if s.book == "Betfair" and family == "h2h":
            meta = s.meta or {}
            if meta.get("total_matched") is not None:
                money["matched"] = float(meta["total_matched"])
            back, lay = meta.get("back_size"), meta.get("lay_size") or meta.get("lay")
            if back and lay:
                money.setdefault("wom", {})[side] = float(back) / (float(back) + float(lay))
    return markets, money, extras


async def market_flow(
    session: AsyncSession, events: list[Event], *,
    now: dt.datetime | None = None, window_hours: float = 8.0, buckets: int = 16,
) -> dict[str, Any]:
    """Where the money and the line have moved over time, for the h2h.

    Reconstructs the blended sharp line at ``buckets`` evenly-spaced moments
    across the window (latest quote per source at/before each moment, then
    blended) — so the series is the SHARP consensus over time, not one book.
    Plus Betfair's matched-volume curve (money flowing in) and each side's
    open→now drift. Empty when nothing was captured."""
    from sportsdata_agents.operations.monitoring import _market_family, _split_selection

    if not events:
        return {}
    ext_ids = {e.external_id for e in events}
    now = now or dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(hours=window_hours)
    snaps = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.event_external_id.in_(ext_ids),
            OddsSnapshot.captured_at >= start,
        ).order_by(OddsSnapshot.captured_at.asc()))).scalars().all()
    # keep only h2h; index by (source, side) -> [(t, odds)], + Betfair matched
    hist: dict[tuple[str, str], list[tuple[dt.datetime, float]]] = {}
    matched: list[tuple[dt.datetime, float]] = []

    def _aware(t: dt.datetime) -> dt.datetime:
        return t if t.tzinfo else t.replace(tzinfo=dt.UTC)

    for snap in snaps:
        if _market_family(snap.market) != "h2h":
            continue
        side, line = _split_selection(str(snap.selection).lower())
        if line is not None or side not in _SIDES:
            continue
        try:
            odds = float(snap.odds)
        except (TypeError, ValueError):
            continue
        cap = _aware(snap.captured_at)
        if odds > 1.0:
            hist.setdefault((snap.book, side), []).append((cap, odds))
        if snap.book == "Betfair" and (snap.meta or {}).get("total_matched") is not None:
            matched.append((cap, float(snap.meta["total_matched"])))
    if not hist:
        return {}
    span = max((now - start).total_seconds(), 1.0)
    times = [start + dt.timedelta(seconds=span * i / (buckets - 1)) for i in range(buckets)]

    def _asof(ser: list[tuple[dt.datetime, float]], t: dt.datetime) -> float | None:
        val = None
        for ts, v in ser:  # ascending; last one at/before t
            if ts <= t:
                val = v
            else:
                break
        return val

    series: list[dict[str, Any]] = []
    for t in times:
        by_source: dict[str, dict[str, float]] = {}
        for (book, side), ser in hist.items():
            o = _asof(ser, t)
            if o is not None:
                by_source.setdefault(book, {})[side] = o
        blended = sharp_line(by_source)["fair"]
        if blended:
            series.append({"t": t.isoformat(),
                           **{side: round(blended.get(side, 0.0), 4) for side in _SIDES if side in blended}})
    matched_series = [{"t": t.isoformat(), "matched": v} for t, v in matched]
    moves: dict[str, dict[str, float]] = {}
    if len(series) >= 2:
        first, last = series[0], series[-1]
        for side in _SIDES:
            if side in first and side in last:
                moves[side] = {"open": first[side], "now": last[side],
                               "delta": round(last[side] - first[side], 4)}
    matched_delta = None
    if matched:
        recent = [v for t, v in matched if t >= now - dt.timedelta(minutes=60)]
        if recent:
            matched_delta = round(max(recent) - min(v for _t, v in matched), 0)
    return {"sharp_series": series, "matched_series": matched_series,
            "moves": moves, "matched_now": matched[-1][1] if matched else None,
            "matched_delta_60m": matched_delta, "window_hours": window_hours}


def _priced_markets(
    markets: dict[tuple[str, float | None], dict[str, dict[str, float]]],
) -> list[dict[str, Any]]:
    """Run the sharp line over every assembled market, sorted h2h → totals →
    lines, most-covered first. Only markets a sharp actually priced survive."""
    out: list[dict[str, Any]] = []
    for (family, line), by_source in markets.items():
        res = sharp_line(by_source)
        if not res["fair"]:  # no sharp priced it -> not a sharp market
            continue
        out.append({
            "key": _market_key(family, line), "family": family, "line": line,
            "label": _market_label(family, line),
            "fair": res["fair"], "sharp_sources": res["sharp_sources"],
            "value": res["value"], "quotes": dict(by_source),
            "n_sharp": len(res["sharp_sources"]),
        })
    fam_order = {"h2h": 0, "total": 1, "line": 2}
    out.sort(key=lambda m: (fam_order.get(m["family"], 9), -m["n_sharp"],
                            abs(m["line"]) if m["line"] is not None else 0))
    return out[:_MAX_MARKETS]


async def _engine_rating(session: AsyncSession, sport: str, fixture: Fixture) -> dict[str, float] | None:
    rows = (await session.execute(
        select(Prediction.selection, Prediction.prob)
        .join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
        .where(ModelArtifact.name == f"engine-ratings:{sport}",
               Prediction.event_external_id == str(fixture.id),
               Prediction.market == "h2h")
        .order_by(Prediction.predicted_at.desc()))).all()
    out: dict[str, float] = {}
    for sel, prob in rows:
        side = str(sel).lower()
        if side in _SIDES:
            out.setdefault(side, float(prob))
    return out or None


def _teams(name: str) -> tuple[str, str]:
    for sep in (" v ", " vs ", " @ ", " - "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return name, ""


async def list_games(
    session: AsyncSession, *, hours: float = 12.0, now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Every upcoming game (all leagues; racing excluded) with a priced h2h,
    summarised: coverage, market count, favourite, Betfair money."""
    from sportsdata_agents.operations.resolution.resolver import split_sides

    now = now or dt.datetime.now(dt.UTC)
    fixtures = [
        f for f in (await session.execute(
            select(Fixture).where(Fixture.start_time >= now,
                                  Fixture.start_time <= now + dt.timedelta(hours=hours))
            # every future event is welcome; classification is in-memory off
            # one bulk query, so the bound is generous (starving out Thursday
            # AFL behind 1200 same-day table-tennis fixtures was the bug)
            .order_by(Fixture.start_time).limit(5000))
        ).scalars()
        # real two-sided matches only — drops player props / novelty specials
        # ("Trea Turner (Home Runs)", "Correct Score") that resolve to h2h noise
        if f.sport not in RACING_SPORTS and split_sides(f.name or "") is not None
    ]
    events = await _fixture_events(session, {f.id for f in fixtures})
    # ONE bulk odds fetch for every fixture's events, grouped in memory —
    # per-fixture queries made anything past a few hundred fixtures unservable.
    all_ext = {e.external_id for evs in events.values() for e in evs}
    floor = now - dt.timedelta(minutes=20)
    snaps_by_ext: dict[str, list[OddsSnapshot]] = {}
    if all_ext:
        for snap in (await session.execute(
                select(OddsSnapshot).where(
                    OddsSnapshot.event_external_id.in_(all_ext),
                    OddsSnapshot.captured_at >= floor,
                    OddsSnapshot.captured_at <= now,
                ).order_by(OddsSnapshot.captured_at.desc()))).scalars():
            snaps_by_ext.setdefault(snap.event_external_id, []).append(snap)
    out: list[dict[str, Any]] = []
    for f in fixtures:
        fx_snaps = [r for e in events.get(f.id, []) for r in snaps_by_ext.get(e.external_id, [])]
        markets, money, _extras = _classify_snaps(fx_snaps)
        priced = _priced_markets(markets)
        h2h = next((m for m in priced if m["family"] == "h2h"), None)
        if h2h is None:
            continue  # detail shows sharp-priced markets only — no sharp, no row
        fair = h2h["fair"]
        home, away = _teams(f.name)
        fav = max(fair, key=lambda s: fair[s]) if fair else None
        n_books = len({b for m in priced for b in m["quotes"] if b not in SHARP_SOURCES})
        out.append({
            "fixture_id": str(f.id), "sport": f.sport, "name": f.name,
            "home": home, "away": away,
            "start_time": f.start_time.isoformat() if f.start_time else None,
            "sharp_sources": h2h["sharp_sources"], "market_count": len(priced),
            "book_count": n_books, "bf_matched": money.get("matched"),
            "favourite": fav, "fav_prob": round(fair[fav], 3) if fav else None,
        })
    out.sort(key=lambda g: str(g.get("start_time") or ""))
    return out


async def game_detail(session: AsyncSession, fixture_id: str,
                      *, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Full detail: the h2h sharp line as the headline, plus every other priced
    market (totals, spreads, alt lines), Betfair money and engine rating."""
    import uuid as _uuid

    try:
        fid = _uuid.UUID(fixture_id)
    except ValueError:
        return None
    f = (await session.execute(select(Fixture).where(Fixture.id == fid))).scalar()
    if f is None:
        return None
    events = await _fixture_events(session, {f.id})
    fx_events = events.get(f.id, [])
    markets, money, _extras = await _markets_by_source(session, fx_events, now=now)
    priced = _priced_markets(markets)
    # Sharp-priced markets ONLY, with every retail book's quotes matched onto
    # them (the quotes grid). The book-only tier and the raw props/extras
    # section were tried and rolled back: they flooded the panel with
    # unstructured non-two-way markets. Revisit behind curation if wanted.
    h2h = next((m for m in priced if m["family"] == "h2h"), None)
    rating = await _engine_rating(session, f.sport, f)
    flow = await market_flow(session, fx_events, now=now)
    home, away = _teams(f.name)
    return {
        "fixture_id": str(f.id), "sport": f.sport, "name": f.name,
        "home": home, "away": away,
        "start_time": f.start_time.isoformat() if f.start_time else None,
        "fair": h2h["fair"] if h2h else {},
        "sharp_sources": h2h["sharp_sources"] if h2h else [],
        "value": h2h["value"] if h2h else {},
        "quotes": h2h["quotes"] if h2h else {},
        "markets": priced,          # sharp-priced only; book quotes matched on
        "bf_money": money, "engine_rating": rating,
        "flow": flow,               # sharp line + Betfair money over time
    }


async def list_specials(
    session: AsyncSession, *, days: float = 1500.0, limit: int = 80,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Novelty and outright markets — elections, entertainment, economics,
    crypto, sports futures: every fixture list_games' two-sided gate drops.

    Presentation is deliberately simpler than the games board: no sharp-line
    blending (a one-sided market has no home/away to de-vig against), just the
    latest price per selection per book, favourites first. The `no <side>`
    complements prediction markets emit are folded away — the affirmative side
    carries the same information."""
    from sportsdata_agents.operations.resolution.resolver import split_sides

    now = now or dt.datetime.now(dt.UTC)
    # Prediction-market events only. Books also emit one-sided names, but those
    # are player props and derivative sub-markets of GAMES ("Total Hits
    # Allowed"), which would flood this list — they belong on the game detail,
    # not here. Kalshi/Polymarket events are novelty by construction: politics,
    # entertainment, economics, crypto, and sports outrights.
    fixture_ids = {
        row[0] for row in (await session.execute(
            select(Event.fixture_id).where(Event.provider.in_(("kalshi", "polymarket")),
                                           Event.fixture_id.is_not(None)))).all()
    }
    # Markets, not matches: most novelty fixtures carry NO start_time — the
    # date that matters is end_time, when the market resolves. Filter and sort
    # on whichever exists (an election "starts" when it settles).
    when = func.coalesce(Fixture.start_time, Fixture.end_time)
    candidates = [
        f for f in (await session.execute(
            select(Fixture).where(Fixture.id.in_(fixture_ids),
                                  when >= now - dt.timedelta(hours=1),
                                  when <= now + dt.timedelta(days=days))
            .order_by(when))
        ).scalars()
        if f.sport not in RACING_SPORTS and split_sides(f.name or "") is None
    ]
    # Soonest-first alone lets high-frequency dailies (crypto hourlies, stock
    # closes) crowd out the marquee long-horizon markets — cap each category
    # so elections and entertainment surface beside them.
    per_cat: dict[str, int] = {}
    fixtures = []
    for f in candidates:
        if per_cat.get(f.sport, 0) >= 8:
            continue
        per_cat[f.sport] = per_cat.get(f.sport, 0) + 1
        fixtures.append(f)
        if len(fixtures) >= limit:
            break
    if not fixtures:
        return []
    events = await _fixture_events(session, {f.id for f in fixtures})

    out: list[dict[str, Any]] = []
    for f in fixtures:
        fx_events = events.get(f.id, [])
        if not fx_events:
            continue
        keys = [(e.provider, e.external_id) for e in fx_events]
        rows = (await session.execute(
            select(OddsSnapshot)
            .where(tuple_(OddsSnapshot.provider, OddsSnapshot.event_external_id).in_(keys),
                   OddsSnapshot.captured_at >= now - dt.timedelta(hours=48))
            .order_by(OddsSnapshot.captured_at))).scalars().all()
        latest: dict[tuple[str, str], Any] = {}
        for r in rows:  # ordered ascending, so the last write per key wins
            latest[(r.book, r.selection)] = r
        sels: dict[str, dict[str, float]] = {}
        for (book, sel), r in latest.items():
            if sel.startswith("no "):
                continue  # the affirmative side carries the same information
            try:
                odds = float(r.odds)
            except (TypeError, ValueError):
                continue
            if odds > 1.0:
                sels.setdefault(sel, {})[book] = odds
        if not sels:
            continue
        best = sorted(
            ({"selection": sel, "best_odds": min(b.values()), "books": len(b),
              "prices": dict(sorted(b.items(), key=lambda kv: kv[1]))}
             for sel, b in sels.items()),
            # float() is for mypy, not maths: the dict literal infers object values,
            # and object is not orderable. Odds are 3dp — the cast cannot reorder them.
            key=lambda x: float(x["best_odds"]))  # type: ignore[arg-type]
        resolves = f.start_time or f.end_time
        out.append({
            "fixture_id": str(f.id), "name": f.name, "category": f.sport,
            "start_time": resolves.isoformat() if resolves else None,
            "is_resolution_time": f.start_time is None,
            "selections": best[:20], "n_selections": len(best),
            "sources": sorted({b for s in sels.values() for b in s}),
        })
    return out


async def special_detail(
    session: AsyncSession, fixture_id: str, *, history_days: float = 14.0,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """One novelty market in depth: current price per book per selection, and
    an hourly best-price series so the panel can show how the market has moved
    — for a prediction market, price movement IS the story."""
    import uuid as _uuid

    try:
        fid = _uuid.UUID(fixture_id)
    except ValueError:
        return None
    f = (await session.execute(select(Fixture).where(Fixture.id == fid))).scalar()
    if f is None:
        return None
    now = now or dt.datetime.now(dt.UTC)
    events = (await session.execute(
        select(Event).where(Event.fixture_id == f.id))).scalars().all()
    keys = [(e.provider, e.external_id) for e in events]
    if not keys:
        return None
    snaps = (await session.execute(
        select(OddsSnapshot)
        .where(tuple_(OddsSnapshot.provider, OddsSnapshot.event_external_id).in_(keys),
               OddsSnapshot.captured_at >= now - dt.timedelta(days=history_days))
        .order_by(OddsSnapshot.captured_at))).scalars().all()

    current: dict[str, dict[str, float]] = {}
    hourly: dict[str, dict[str, float]] = {}   # selection -> bucket_iso -> best odds
    for r in snaps:
        sel = str(r.selection)
        if sel.startswith("no "):
            continue
        try:
            odds = float(r.odds)
        except (TypeError, ValueError):
            continue
        if odds <= 1.0:
            continue
        current.setdefault(sel, {})[r.book] = odds   # ascending order: last write wins
        bucket = r.captured_at.replace(minute=0, second=0, microsecond=0).isoformat()
        buckets = hourly.setdefault(sel, {})
        buckets[bucket] = max(buckets.get(bucket, 0.0), odds)

    sels = sorted(
        ({"selection": sel,
          "prices": dict(sorted(books.items(), key=lambda kv: kv[1])),
          "best_odds": min(books.values()),
          "series": sorted(hourly.get(sel, {}).items())}
         for sel, books in current.items()),
        key=lambda x: float(x["best_odds"]))  # type: ignore[arg-type]
    resolves = f.start_time or f.end_time
    return {
        "fixture_id": str(f.id), "name": f.name, "category": f.sport,
        "start_time": resolves.isoformat() if resolves else None,
        "is_resolution_time": f.start_time is None,
        "selections": sels[:24],
        "sources": sorted({b for v in current.values() for b in v}),
    }
