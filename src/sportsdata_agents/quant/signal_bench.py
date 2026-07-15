"""Signal bench: score RAW signals against settled outcomes — the Alpha-Zoo
idea (bench factors by information coefficient) translated to racing.

Every alert kind is a packaged decision; underneath sit raw signals we
capture whether or not anything fires. The bench asks, per signal: across
settled races, does it correlate with runners doing BETTER THAN THE MARKET
EXPECTED? The target is the market residual ``won - implied``, where
``implied = 1/odds`` at T-5 minutes — so a signal only scores for knowing
something the closing market didn't already price.

Signals (racing win markets, per provider-book so no cross-book join):

- ``steam_60m``  — pct price change T-60m -> T-5m (negative = firming).
  If money knows, firming runners should over-perform the market residual.
- ``engine_gap`` — engine-form probability x market odds - 1 (the model's
  claimed edge). If the form engine is real, positive gaps should carry
  positive residuals.
- ``market_implied`` — the control: 1/odds at T-5m. Against the residual
  target it should be ~0 by construction; a consistent negative reading is
  the favourite-longshot bias showing up in our own captures.

IC is the Pearson correlation of signal vs residual pooled across runners,
with a t-stat (r * sqrt((n-2)/(1-r^2))) and a tercile breakdown so a
non-linear signal still shows its shape. |t| >= 2 is the "worth a look"
line, not proof.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import (
    EventResult,
    ModelArtifact,
    OddsSnapshot,
    Prediction,
    Price,
)

__all__ = ["format_signal_bench", "signal_bench"]

_STEAM_FROM = dt.timedelta(minutes=60)
_AS_OF = dt.timedelta(minutes=5)


def _pearson(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """(r, t_stat) or None below the minimum sample."""
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    r = sxy / math.sqrt(sxx * syy)
    r = max(min(r, 0.999999), -0.999999)
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    return r, t


def _terciles(pairs: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Mean residual by signal tercile — the monotonicity read."""
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    out = []
    for i in range(3):
        chunk = ordered[i * n // 3:(i + 1) * n // 3]
        if chunk:
            out.append({"n": len(chunk),
                        "mean_residual": round(
                            sum(y for _x, y in chunk) / len(chunk), 4)})
    return out


async def _price_asof(
    session: AsyncSession, *, provider: str, book: str, event_id: str,
    selection: str, at: dt.datetime,
) -> float | None:
    row = (await session.execute(
        select(Price.odds).where(
            Price.provider == provider, Price.book == book,
            Price.event_external_id == event_id, Price.market == "win",
            Price.selection == selection, Price.changed_at <= at,
        ).order_by(Price.changed_at.desc()).limit(1))).scalar()
    value = float(row) if row is not None else None
    return value if value and value > 1.0 else None


async def signal_bench(
    session: AsyncSession,
    *,
    days: float = 14.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=days)

    # settled_at is stamped by the results run but nullable — fall back to the
    # race's start time so unstamped rows aren't silently outside every window
    results = (await session.execute(
        select(EventResult).where(
            or_(EventResult.settled_at >= since,
                and_(EventResult.settled_at.is_(None),
                     EventResult.start_time >= since)))
    )).scalars().all()
    winners = {(r.provider, r.event_external_id): str(r.winning_selection)
               for r in results if str(r.winning_selection).isdigit()}

    # engine-form predictions, newest per (event, selection)
    engine_prob: dict[tuple[str, str], float] = {}
    if winners:
        rows = (await session.execute(
            select(Prediction.event_external_id, Prediction.selection,
                   Prediction.prob, Prediction.predicted_at)
            .join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
            .where(ModelArtifact.name == "engine-form:racing",
                   Prediction.market == "win")
            .order_by(Prediction.predicted_at.desc()))).all()
        for event_id, selection, prob, _at in rows:
            engine_prob.setdefault((str(event_id), str(selection)), float(prob))

    # (signal value, residual) pairs per signal
    pairs: dict[str, list[tuple[float, float]]] = {
        "steam_60m": [], "engine_gap": [], "market_implied": []}
    races_used = 0

    for (provider, event_id), winner in winners.items():
        snaps = (await session.execute(
            select(OddsSnapshot).where(
                OddsSnapshot.provider == provider,
                OddsSnapshot.event_external_id == event_id,
                OddsSnapshot.market == "win"))).scalars().all()
        # one row per (book, selection); need runner numbers + a start time
        start = next((s.start_time for s in snaps if s.start_time), None)
        if start is None:
            continue
        as_of = start - _AS_OF
        open_at = start - _STEAM_FROM
        race_counted = False
        for snap in snaps:
            number = (snap.meta or {}).get("runner_number")
            if number is None:
                continue
            closing = await _price_asof(
                session, provider=provider, book=snap.book, event_id=event_id,
                selection=snap.selection, at=as_of)
            if closing is None:
                continue
            implied = 1.0 / closing
            residual = (1.0 if str(number) == winner else 0.0) - implied
            race_counted = True
            pairs["market_implied"].append((implied, residual))
            opening = await _price_asof(
                session, provider=provider, book=snap.book, event_id=event_id,
                selection=snap.selection, at=open_at)
            if opening is not None:
                pairs["steam_60m"].append(
                    ((closing - opening) / opening * 100.0, residual))
            prob = engine_prob.get((event_id, str(number)))
            if prob is not None and 0.0 < prob < 1.0:
                pairs["engine_gap"].append((prob * closing - 1.0, residual))
        if race_counted:
            races_used += 1

    signals: dict[str, Any] = {}
    for name, pts in pairs.items():
        stat = _pearson([x for x, _y in pts], [y for _x, y in pts])
        signals[name] = {
            "n": len(pts),
            "ic": round(stat[0], 4) if stat else None,
            "t_stat": round(stat[1], 2) if stat else None,
            "terciles": _terciles(pts) if len(pts) >= 10 else [],
        }
    return {"since": since.isoformat(), "until": now.isoformat(),
            "races": races_used, "signals": signals}


def format_signal_bench(report: dict[str, Any]) -> str:
    lines = [f":microscope: Signal bench — {report['races']} settled races "
             f"({report['since'][:10]} → {report['until'][:10]})"]
    for name, s in (report.get("signals") or {}).items():
        if s["ic"] is None:
            lines.append(f"  {name}: n={s['n']} — below the sample floor")
            continue
        verdict = ("worth a look" if abs(s["t_stat"]) >= 2.0 else "noise so far")
        lines.append(f"  {name}: IC {s['ic']:+.3f} (t {s['t_stat']:+.1f}, "
                     f"n={s['n']}) — {verdict}")
        if s["terciles"]:
            lines.append("    terciles (mean residual): " + " · ".join(
                f"{t['mean_residual']:+.3f}" for t in s["terciles"]))
    lines.append("_Target is won - market-implied at T-5m: a signal scores "
                 "only for beating the closing market, not for liking "
                 "favourites._")
    return "\n".join(lines)
