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
from typing import Any

from sqlalchemy import select
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
) -> tuple[dict[tuple[str, float | None], dict[str, dict[str, float]]], dict[str, Any]]:
    """{(family, line): {source: {side: odds}}} across h2h/total/line for the
    freshest snapshot per (source, market, side), plus Betfair money."""
    from sportsdata_agents.operations.monitoring import _market_family, _split_selection

    if not events:
        return {}, {}
    ext_ids = {e.external_id for e in events}
    now = now or dt.datetime.now(dt.UTC)
    floor = now - dt.timedelta(minutes=fresh_minutes)
    snaps = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.event_external_id.in_(ext_ids),
            OddsSnapshot.captured_at >= floor,
            OddsSnapshot.captured_at <= now,  # freshest snapshot AS OF `now` (no-op live; rewinds for replay capture)
        ).order_by(OddsSnapshot.captured_at.desc()))).scalars().all()
    markets: dict[tuple[str, float | None], dict[str, dict[str, float]]] = {}
    seen: set[tuple[str, str, float | None, str]] = set()
    money: dict[str, Any] = {}
    for s in snaps:
        family = _market_family(s.market)
        if family not in ("h2h", "total", "line"):
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
    return markets, money


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
    now = now or dt.datetime.now(dt.UTC)
    fixtures = [
        f for f in (await session.execute(
            select(Fixture).where(Fixture.start_time >= now,
                                  Fixture.start_time <= now + dt.timedelta(hours=hours)))
        ).scalars()
        if f.sport not in RACING_SPORTS
    ]
    events = await _fixture_events(session, {f.id for f in fixtures})
    out: list[dict[str, Any]] = []
    for f in fixtures:
        markets, money = await _markets_by_source(session, events.get(f.id, []), now=now)
        priced = _priced_markets(markets)
        h2h = next((m for m in priced if m["family"] == "h2h"), None)
        if h2h is None:
            continue  # a game with no sharp h2h isn't a board row
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
    markets, money = await _markets_by_source(session, fx_events, now=now)
    priced = _priced_markets(markets)
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
        "markets": priced,          # every priced market (h2h first)
        "bf_money": money, "engine_rating": rating,
        "flow": flow,               # sharp line + Betfair money over time
    }
