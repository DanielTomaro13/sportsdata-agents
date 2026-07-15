"""Export settled fixtures as engines-replay fodder (Phase 3 of the proving
plan): one JSON object per fixture, shaped exactly like the engines package's
``ReplayFixture`` kwargs, so two weeks of warehouse captures can re-fit
per-sport dispersion/pace and re-issue EDGE-VERDICT.

Per fixture: the anchor book's h2h + main-total quotes as of T (default 60
minutes before the jump) seed the engine; the same keys re-read at the start
become ``close_quotes``; the recorded score (frame-translated onto the
fixture, the scoreboard's rule) settles. Fixtures without a complete anchor
menu or a parseable score are skipped and counted — a replay that quietly
drops fixtures overstates itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, Fixture, OddsSnapshot, Price

__all__ = ["export_replay_fixtures"]

# warehouse fixture.sport -> the engines package's sport module name
_ENGINE_SPORT = {
    "australian_rules": "afl",
    "afl": "afl",
    "rugby_league": "rugby_league",
    "rugby_union": "rugby_union",
    "basketball": "basketball",
    "baseball": "baseball",
    "ice_hockey": "ice_hockey",
    "soccer": "soccer",
    "american_football": "nfl",
    "nfl": "nfl",
    "cricket": "cricket",
}

_ANCHOR_BOOKS = ("Pinnacle", "Unibet", "Sportsbet", "TAB", "BetR", "FanDuel")


async def _book_menu(
    session: AsyncSession, provider: str, event_id: str, as_of: dt.datetime
) -> dict[str, Any] | None:
    """The book's normalised full-game menu as of a moment: h2h home/away and
    the most balanced total (line, over, under). None when incomplete."""
    from sportsdata_agents.operations.monitoring import (
        _market_family,
        _split_selection,
    )

    rows = (await session.execute(
        select(Price).where(
            Price.provider == provider,
            Price.event_external_id == event_id,
            Price.changed_at <= as_of,
        ).order_by(Price.changed_at.desc()).limit(400)
    )).scalars().all()
    h2h: dict[str, float] = {}
    totals: dict[float, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.market, row.selection)
        if key in seen:
            continue
        seen.add(key)
        family = _market_family(row.market)
        side, line = _split_selection(row.selection.lower())
        if family == "h2h" and line is None and side in ("home", "away"):
            h2h.setdefault(side, float(row.odds))
        elif family == "total" and side in ("over", "under") and line is not None:
            totals.setdefault(line, {})[side] = float(row.odds)
    paired = {ln: p for ln, p in totals.items() if len(p) == 2}
    if "home" not in h2h or "away" not in h2h or not paired:
        return None
    main = min(paired, key=lambda ln: abs(1.0 / paired[ln]["over"]
                                          - 1.0 / paired[ln]["under"]))
    return {"h2h": h2h, "total_line": main,
            "over": paired[main]["over"], "under": paired[main]["under"]}


def _quote_rows(menu: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"market": "h2h", "selection": "home", "odds": menu["h2h"]["home"]},
        {"market": "h2h", "selection": "away", "odds": menu["h2h"]["away"]},
        {"market": "total", "selection": "over", "line": menu["total_line"],
         "odds": menu["over"]},
        {"market": "total", "selection": "under", "line": menu["total_line"],
         "odds": menu["under"]},
    ]


def _flip(menu: dict[str, Any]) -> dict[str, Any]:
    """The book listed the teams the other way round — swap h2h sides
    (totals are frame-free)."""
    return {**menu, "h2h": {"home": menu["h2h"]["away"],
                            "away": menu["h2h"]["home"]}}


async def export_replay_fixtures(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: float = 14.0,
    sports: tuple[str, ...] | None = None,
    minutes_before: float = 60.0,
) -> dict[str, Any]:
    """{"fixtures": [ReplayFixture kwargs...], "skipped": {reason: n}}."""
    from sportsdata_agents.quant.backtest import _translate_side
    from sportsdata_agents.quant.scoreboard import _fixture_scores

    now = dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=days)
    async with session_factory() as session:
        wanted = set(sports or _ENGINE_SPORT)
        fixtures = [
            f for f in (await session.execute(
                select(Fixture).where(Fixture.start_time >= since,
                                      Fixture.start_time <= now))).scalars()
            if f.sport in wanted and f.sport in _ENGINE_SPORT
        ]
        scores = await _fixture_scores(session, {f.id for f in fixtures})
        out: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}

        def _skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for fixture in fixtures:
            score = scores.get(fixture.id)
            if score is None:
                _skip("no_score")
                continue
            siblings = (await session.execute(
                select(Event).where(Event.fixture_id == fixture.id)
            )).scalars().all()
            start = fixture.start_time
            if start is None:
                _skip("no_start")
                continue
            as_of = start - dt.timedelta(minutes=minutes_before)
            menu = close = None
            for book in _ANCHOR_BOOKS:
                for sib in siblings:
                    candidate = await _book_menu(
                        session, sib.provider, sib.external_id, as_of)
                    if candidate is None:
                        continue
                    name = (await session.execute(
                        select(OddsSnapshot.event_name).where(
                            OddsSnapshot.provider == sib.provider,
                            OddsSnapshot.event_external_id == sib.external_id,
                            OddsSnapshot.book == book,
                            OddsSnapshot.event_name != "",
                        ).limit(1))).scalar()
                    if name is None:
                        continue  # this sibling isn't the anchor book's
                    orientation = _translate_side("home", fixture.name, name)
                    if orientation is None:
                        continue  # never seed the engine with a maybe-flipped h2h
                    if orientation == "away":
                        candidate = _flip(candidate)
                    close_menu = await _book_menu(
                        session, sib.provider, sib.external_id, start)
                    if close_menu is not None and orientation == "away":
                        close_menu = _flip(close_menu)
                    menu, close = candidate, close_menu
                    break
                if menu is not None:
                    break
            if menu is None:
                _skip("no_anchor_menu")
                continue
            out.append({
                "sport": _ENGINE_SPORT[fixture.sport],
                "fixture_id": str(fixture.id),
                "quotes": {"h2h": [menu["h2h"]["home"], menu["h2h"]["away"]],
                           "total": [menu["total_line"], menu["over"],
                                     menu["under"]]},
                "taken_quotes": _quote_rows(menu),
                "close_quotes": _quote_rows(close) if close else [],
                "result": {"home_score": score[0], "away_score": score[1]},
            })
        # the RUN CARD makes replays comparable across weeks: same knobs +
        # same window shape => apples to apples; a drifted fingerprint says
        # "you're not comparing like with like" before the metrics lie to you
        per_sport: dict[str, int] = {}
        for row in out:
            per_sport[row["sport"]] = per_sport.get(row["sport"], 0) + 1
        config = {"days": days, "minutes_before": minutes_before,
                  "sports": sorted(wanted & set(_ENGINE_SPORT)),
                  "anchor_books": list(_ANCHOR_BOOKS)}
        fingerprint = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
        run_card = {
            "generated_at": now.isoformat(),
            "window": {"since": since.isoformat(), "until": now.isoformat()},
            "config": config,
            "config_fingerprint": fingerprint,
            "exported": len(out),
            "per_sport": dict(sorted(per_sport.items())),
            "skipped": dict(sorted(skipped.items())),
            "coverage_pct": (round(100.0 * len(out) /
                                   (len(out) + sum(skipped.values())), 1)
                             if (out or skipped) else None),
        }
        return {"fixtures": out, "skipped": skipped, "run_card": run_card}
