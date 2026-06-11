"""Results ingestion (resolution milestone): racing AND league sports settle here.

Racing: PointsBet's meetings listing carries ``placing`` ("3,8,10,1") once a race is
run — the same call the racing feed already makes, so settlement costs ZERO extra
requests. League sports: official scoreboards (NBA live scoreboard, AFL matches
list, NRL fixture) carry final scores — winners land as "home"/"away"/"draw" in the
SCOREBOARD's frame, with the event name stored in meta so cross-book settlement can
translate orientation. Scoreboard events are mapped onto existing fixtures (never
founding one — a result no book priced settles nothing).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import EventResult

logger = logging.getLogger(__name__)


async def record_results(
    session_factory: async_sessionmaker[AsyncSession], results: list[dict[str, Any]]
) -> int:
    """Upsert winners: [{provider, sport, event_external_id, winning_selection,
    start_time?, meta?}] — re-recording corrects, never duplicates."""
    written = 0
    async with session_factory() as session:
        for row in results:
            event_id = str(row["event_external_id"])
            existing = (
                (await session.execute(select(EventResult).where(EventResult.event_external_id == event_id)))
                .scalars()
                .first()
            )
            if existing is not None:
                if existing.winning_selection == str(row["winning_selection"]):
                    continue
                existing.winning_selection = str(row["winning_selection"])
                existing.settled_at = dt.datetime.now(dt.UTC)
            else:
                session.add(EventResult(
                    provider=str(row.get("provider", "")),
                    sport=str(row.get("sport", "racing")),
                    event_external_id=event_id,
                    winning_selection=str(row["winning_selection"]),
                    start_time=row.get("start_time"),
                    settled_at=dt.datetime.now(dt.UTC),
                    meta=row.get("meta") or {},
                ))
            written += 1
        await session.commit()
    return written


# racing call sites predate the league leg — same upsert
record_race_results = record_results


async def ingest_racing_results(
    manager: Any, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """PointsBet meetings → resulted races' placings → event_results (winner = the
    first saddle number in ``placing``)."""
    now = dt.datetime.now(dt.UTC)
    meetings = await manager.call_tool("pointsbet_racing_meetings", {
        "startDate": now.strftime("%Y-%m-%dT00:00:00.000Z"),
        "endDate": (now + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z"),
    })
    results: list[dict[str, Any]] = []
    for group in meetings if isinstance(meetings, list) else []:
        for meeting in group.get("meetings", []) or []:
            for race in meeting.get("races", []) or []:
                placing = str(race.get("placing") or "").strip()
                if not placing or race.get("raceId") is None:
                    continue
                winner = placing.split(",", 1)[0].strip()
                if winner:
                    results.append({
                        "provider": "pointsbet_racing",
                        "sport": "racing",
                        "event_external_id": str(race["raceId"]),
                        "winning_selection": winner,
                    })
    written = await record_race_results(session_factory, results)
    logger.info("racing results: %d settled", written)
    return written


# ─── league-sport results from official scoreboards (shapes captured live
#     2026-06-11): winner is home/away/draw in the SCOREBOARD's frame; the event
#     name rides meta so settlement can translate orientation across books ──────


def _winner(home_score: Any, away_score: Any) -> str | None:
    try:
        h, a = float(home_score), float(away_score)
    except (TypeError, ValueError):
        return None
    return "home" if h > a else "away" if a > h else "draw"


def _result_row(
    *, provider: str, sport: str, external_id: str, home: str, away: str,
    home_score: Any, away_score: Any, start: Any,
) -> dict[str, Any] | None:
    winner = _winner(home_score, away_score)
    if not winner or not external_id:
        return None
    from sportsdata_agents.operations.ingestion.store import _parse_start

    return {
        "provider": provider,
        "sport": sport,
        "event_external_id": external_id,
        "winning_selection": winner,
        "start_time": _parse_start(start),
        "meta": {"event_name": f"{home} v {away}",
                 "score": f"{home_score}-{away_score}"},
    }


async def _nba_results(manager: Any) -> list[dict[str, Any]]:
    """nba_scoreboard_today: gameStatus 3 = final; team names are city + nickname."""
    board = await manager.call_tool("nba_scoreboard_today", {})
    rows: list[dict[str, Any]] = []
    for game in ((board.get("scoreboard") or {}).get("games")) or []:
        if game.get("gameStatus") != 3:
            continue
        home, away = game.get("homeTeam") or {}, game.get("awayTeam") or {}
        row = _result_row(
            provider="nba_stats", sport="basketball",
            external_id=str(game.get("gameId") or ""),
            home=f"{home.get('teamCity', '')} {home.get('teamName', '')}".strip(),
            away=f"{away.get('teamCity', '')} {away.get('teamName', '')}".strip(),
            home_score=home.get("score"), away_score=away.get("score"),
            start=game.get("gameTimeUTC"),
        )
        if row:
            rows.append(row)
    return rows


async def _afl_results(manager: Any, *, days_back: int = 8) -> list[dict[str, Any]]:
    """afl_matches_list over a trailing window; CONCLUDED matches carry totalScore."""
    now = dt.datetime.now(dt.UTC)
    page = await manager.call_tool("afl_matches_list", {
        "startDate": (now - dt.timedelta(days=days_back)).date().isoformat(),
        "endDate": now.date().isoformat(),
        "pageSize": 50,
    })
    rows: list[dict[str, Any]] = []
    for match in page.get("matches") or []:
        if match.get("status") != "CONCLUDED":
            continue
        home, away = match.get("home") or {}, match.get("away") or {}
        row = _result_row(
            provider="afl_api", sport="australian_rules",
            external_id=str(match.get("providerId") or match.get("id") or ""),
            home=str((home.get("team") or {}).get("name") or ""),
            away=str((away.get("team") or {}).get("name") or ""),
            home_score=(home.get("score") or {}).get("totalScore"),
            away_score=(away.get("score") or {}).get("totalScore"),
            start=match.get("utcStartTime"),
        )
        if row:
            rows.append(row)
    return rows


async def _nrl_results(manager: Any) -> list[dict[str, Any]]:
    """nrl_competitions → current NRL/Origin competitions → fixture per comp;
    matchStatus "complete" rows carry final squad scores."""
    catalogue = await manager.call_tool("nrl_competitions", {})
    season = str(dt.datetime.now(dt.UTC).year)
    comp_ids = [
        comp["id"]
        for comp in ((catalogue.get("competitionDetails") or {}).get("competition")) or []
        if comp.get("id") is not None
        and season in str(comp.get("season") or "")
        and ("NRL" in str(comp.get("name") or "") or "Origin" in str(comp.get("name") or ""))
    ]
    rows: list[dict[str, Any]] = []
    for comp_id in comp_ids:
        try:
            fixture = await manager.call_tool("nrl_fixture", {"competitionId": comp_id})
        except Exception as e:
            logger.warning("nrl fixture %s failed: %s", comp_id, e)
            continue
        for match in ((fixture.get("fixture") or {}).get("match")) or []:
            if str(match.get("matchStatus") or "").lower() != "complete":
                continue
            row = _result_row(
                provider="nrl_api", sport="rugby_league",
                external_id=str(match.get("matchId") or ""),
                home=str(match.get("homeSquadName") or ""),
                away=str(match.get("awaySquadName") or ""),
                home_score=match.get("homeSquadScore"),
                away_score=match.get("awaySquadScore"),
                start=match.get("utcStartTime"),
            )
            if row:
                rows.append(row)
    return rows


async def ingest_league_results(
    manager: Any, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, Any]:
    """Official scoreboards → event_results + fixture mapping. Each league is
    isolated (one API down never sinks the rest); results only MAP onto fixtures
    books actually priced — unmatched ones still record, keyed by their own id."""
    from sportsdata_agents.operations.resolution.resolver import map_events_to_fixtures

    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for league, collect in (("nba", _nba_results), ("afl", _afl_results), ("nrl", _nrl_results)):
        try:
            found = await collect(manager)
            rows.extend(found)
            report[league] = len(found)
        except Exception as e:
            logger.warning("%s results failed: %s: %s", league, type(e).__name__, e)
            report[league] = f"error: {e}"
    written = await record_results(session_factory, rows)
    mapping = await map_events_to_fixtures(session_factory, [
        {"provider": r["provider"], "external_id": r["event_external_id"],
         "event_name": r["meta"]["event_name"], "sport": r["sport"],
         "event_time": r.get("start_time")}
        for r in rows
    ])
    report.update({"recorded": written, **{f"fixtures_{k}": v for k, v in mapping.items()}})
    logger.info("league results: %s", report)
    return report
