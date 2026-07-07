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
    start_time?, meta?}] — re-recording corrects, never duplicates. Keyed on
    (provider, event id): five providers share one numeric id namespace, and a
    cross-provider collision must never overwrite someone else's result."""
    written = 0
    async with session_factory() as session:
        for row in results:
            event_id = str(row["event_external_id"])
            existing = (
                (await session.execute(select(EventResult).where(
                    EventResult.event_external_id == event_id,
                    EventResult.provider == str(row.get("provider", "")),
                )))
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
                # Settle ONLY on a clean saddle number. Abandoned/void/scratched races put a
                # text marker here (e.g. "ABD"/"VOID"/"NR") — never settle a bet off that;
                # leave the race unsettled. (Dead-heats settle on the listed leader; true
                # dead-heat stake-splitting would need the source to expose tied placings.)
                if not winner.isdigit():
                    logger.debug("racing result skipped — non-numeric placing %r (race %s)",
                                 placing, race.get("raceId"))
                    continue
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
    """afl_matches_list over a trailing window, ALL pages — a busy week (AFL +
    AFLW + state/junior comps) overflows one page of 50; CONCLUDED matches carry
    totalScore."""
    now = dt.datetime.now(dt.UTC)
    rows: list[dict[str, Any]] = []
    page_no = 0
    while True:
        page = await manager.call_tool("afl_matches_list", {
            "startDate": (now - dt.timedelta(days=days_back)).date().isoformat(),
            "endDate": now.date().isoformat(),
            "pageSize": 50,
            "page": page_no,
        })
        for match in page.get("matches") or []:
            if match.get("status") != "CONCLUDED":
                continue
            comp = str((match.get("compSeason") or {}).get("name") or "").lower()
            if not ("afl premiership" in comp or "aflw" in comp):
                # the API window carries VFL/U18/state games too — fitting the
                # AFL ratings on academy scores read a 110-point "fair total"
                # for a 176-point competition (lived: 2026-07-07)
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
        num_pages = int((((page.get("meta") or {}).get("pagination")) or {}).get("numPages") or 1)
        page_no += 1
        if page_no >= num_pages or page_no >= 6:  # safety cap
            break
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


async def _mlb_results(manager: Any, *, days_back: int = 2) -> list[dict[str, Any]]:
    """MLB StatsAPI schedule: codedGameState F = final; first-party (preferred
    over ESPN's MLB scoreboard)."""
    now = dt.datetime.now(dt.UTC)
    sched = await manager.call_tool("mlb_schedule", {
        "startDate": (now - dt.timedelta(days=days_back)).date().isoformat(),
        "endDate": now.date().isoformat(),
    })
    rows: list[dict[str, Any]] = []
    for date_node in sched.get("dates") or []:
        for game in date_node.get("games") or []:
            if str((game.get("status") or {}).get("codedGameState")) != "F":
                continue
            teams = game.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            row = _result_row(
                provider="mlb_api", sport="baseball",
                external_id=str(game.get("gamePk") or ""),
                home=str((home.get("team") or {}).get("name") or ""),
                away=str((away.get("team") or {}).get("name") or ""),
                home_score=home.get("score"), away_score=away.get("score"),
                start=game.get("gameDate"),
            )
            if row:
                rows.append(row)
    return rows


# Every other team sport settles from ESPN's scoreboard (one generic route). The
# aggregator exclusion was about ODDS (second-hand prices); results are facts.
# Leagues with first-party APIs above (NBA, AFL, NRL, MLB) are deliberately absent.
# Extend by adding (sport_slug, league_slug, canonical_sport) rows.
_ESPN_LEAGUES: tuple[tuple[str, str, str], ...] = (
    ("football", "nfl", "american_football"),
    ("football", "college-football", "american_football"),
    ("hockey", "nhl", "ice_hockey"),
    ("basketball", "wnba", "basketball"),
    ("basketball", "mens-college-basketball", "basketball"),
    ("basketball", "womens-college-basketball", "basketball"),
    ("baseball", "college-baseball", "baseball"),
    ("soccer", "eng.1", "soccer"),
    ("soccer", "usa.1", "soccer"),
    ("soccer", "uefa.champions", "soccer"),
    ("soccer", "fifa.world", "soccer"),
    ("soccer", "aus.1", "soccer"),
)


async def _espn_results(manager: Any, *, days_back: int = 2) -> list[dict[str, Any]]:
    """ESPN scoreboards for every catalogued league: completed events carry
    competitors with homeAway + score (strings); draws settle as draws."""
    now = dt.datetime.now(dt.UTC)
    dates = f"{(now - dt.timedelta(days=days_back)):%Y%m%d}-{now:%Y%m%d}"
    rows: list[dict[str, Any]] = []
    for sport_slug, league, sport in _ESPN_LEAGUES:
        try:
            board = await manager.call_tool(
                "espn_scoreboard", {"sport": sport_slug, "league": league, "dates": dates}
            )
        except Exception as e:
            logger.warning("espn %s/%s scoreboard failed: %s", sport_slug, league, e)
            continue
        for event in board.get("events") or []:
            if not (((event.get("status") or {}).get("type")) or {}).get("completed"):
                continue
            competition = (event.get("competitions") or [{}])[0]
            by_side = {
                str(c.get("homeAway")): c
                for c in competition.get("competitors") or []
            }
            home, away = by_side.get("home") or {}, by_side.get("away") or {}
            row = _result_row(
                provider="espn", sport=sport,
                external_id=str(event.get("id") or ""),
                home=str((home.get("team") or {}).get("displayName") or ""),
                away=str((away.get("team") or {}).get("displayName") or ""),
                home_score=home.get("score"), away_score=away.get("score"),
                start=competition.get("date") or event.get("date"),
            )
            if row:
                rows.append(row)
    return rows


async def ingest_league_results(
    manager: Any, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, Any]:
    """Official sources → event_results + fixture mapping. First-party league APIs
    (NBA, AFL, NRL, MLB) are preferred; everything else reverts to ESPN's generic
    scoreboard. Each source is isolated (one API down never sinks the rest);
    results only MAP onto fixtures books actually priced — unmatched ones still
    record, keyed by their own id."""
    from sportsdata_agents.operations.resolution.resolver import map_events_to_fixtures

    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    sources = (
        ("nba", _nba_results), ("afl", _afl_results), ("nrl", _nrl_results),
        ("mlb", _mlb_results), ("espn", _espn_results),
    )
    for league, collect in sources:
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


# ── prediction-market resolutions (Kalshi settled / Polymarket resolved) ────
# The bridge's alerts settle against these: one EventResult per EVENT whose
# winning_selection is the resolved outcome label in OUR selection namespace
# (the same labels the kalshi_all/polymarket_all normalizers emit).


async def ingest_prediction_resolutions(
    manager: Any,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pages: int = 3,
) -> dict[str, int]:
    rows: list[dict[str, Any]] = []

    # Kalshi: settled markets carry result yes/no; the YES-resolved contract's
    # subject is the event's winner (candidate events resolve exactly one YES)
    cursor = None
    for _ in range(pages):
        args: dict[str, Any] = {"status": "settled", "limit": 200}
        if cursor:
            args["cursor"] = cursor
        try:
            page = await manager.call_tool("kalshi_markets", args)
        except Exception as e:
            logger.warning("kalshi settled fetch failed: %s", e)
            break
        for market in page.get("markets", []) or []:
            if str(market.get("result", "")).lower() != "yes":
                continue
            subject = str(market.get("yes_sub_title") or market.get("title") or "").lower().strip()
            event = str(market.get("event_ticker") or "")
            if subject and event:
                rows.append({"provider": "kalshi", "sport": "prediction",
                             "event_external_id": event, "winning_selection": subject,
                             "meta": {"ticker": market.get("ticker")}})
        cursor = page.get("cursor")
        if not cursor:
            break

    # Polymarket: closed events; the market whose outcomePrice hit 1 names the
    # winner (grouped events: the groupItemTitle; plain binaries: the outcome)
    try:
        events = await manager.call_tool("polymarket_events", {
            "closed": True, "limit": 100, "order": "endDate", "ascending": False})
    except Exception as e:
        logger.warning("polymarket closed fetch failed: %s", e)
        events = []
    if isinstance(events, dict):
        events = events.get("result") or []
    import json as _json

    for event in events or []:
        event_id = str(event.get("id") or "")
        winner = None
        for market in event.get("markets", []) or []:
            outcomes = market.get("outcomes")
            prices = market.get("outcomePrices")
            if isinstance(outcomes, str):
                try:
                    outcomes = _json.loads(outcomes)
                except ValueError:
                    continue
            if isinstance(prices, str):
                try:
                    prices = _json.loads(prices)
                except ValueError:
                    continue
            subject = str(market.get("groupItemTitle") or "").strip()
            for name, price in zip(outcomes or [], prices or [], strict=False):
                try:
                    hit = float(price) >= 0.999
                except (TypeError, ValueError):
                    continue
                if not hit:
                    continue
                label = (subject if str(name).lower() == "yes"
                         else f"{name} {subject}") if subject else str(name)
                winner = label.lower().strip()
                break
            if winner and subject:
                break  # grouped event: the YES contract names the winner
        if event_id and winner:
            rows.append({"provider": "polymarket", "sport": "prediction",
                         "event_external_id": event_id, "winning_selection": winner,
                         "meta": {"title": event.get("title")}})

    recorded = await record_results(session_factory, rows)
    return {"kalshi": sum(1 for r in rows if r["provider"] == "kalshi"),
            "polymarket": sum(1 for r in rows if r["provider"] == "polymarket"),
            "recorded": recorded}
