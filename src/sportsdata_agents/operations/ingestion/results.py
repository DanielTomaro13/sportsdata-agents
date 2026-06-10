"""Results ingestion (resolution milestone, first leg): racing settles itself.

PointsBet's racing meetings listing carries ``placing`` ("3,8,10,1") once a race is
run — the same call the racing feed already makes, so settlement costs ZERO extra
requests. Winners land in ``event_results`` keyed by the book's race id; through the
fixtures/events mapping they settle every book's series for that race. League-sport
results (scoreboards) join the same table once their fixtures resolve — next leg.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import EventResult

logger = logging.getLogger(__name__)


async def record_race_results(
    session_factory: async_sessionmaker[AsyncSession], results: list[dict[str, Any]]
) -> int:
    """Upsert race winners: [{provider, sport, event_external_id, winning_selection}]."""
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
                    settled_at=dt.datetime.now(dt.UTC),
                ))
            written += 1
        await session.commit()
    return written


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
