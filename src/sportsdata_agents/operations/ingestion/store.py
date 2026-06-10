"""Warehouse writes + queries (M2.1): snapshots append, prices dedupe to change-points.

Dedupe contract: ``odds_snapshots`` records every observation (prunable);
``prices`` gains a row only when a key's odds MOVE (or on first sighting) — that is
the series line-movement queries and backtests read.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Price
from sportsdata_agents.operations.ingestion.normalizers import PricePoint

logger = logging.getLogger(__name__)


async def record_points(
    session_factory: async_sessionmaker[AsyncSession],
    points: list[PricePoint],
    *,
    captured_at: dt.datetime | None = None,
) -> dict[str, int]:
    """Persist one capture: every point → a snapshot row; moved prices → change-points."""
    captured_at = captured_at or dt.datetime.now(dt.UTC)
    changes = 0
    async with session_factory() as session:
        for p in points:
            session.add(
                OddsSnapshot(
                    captured_at=captured_at,
                    provider=p.provider,
                    book=p.book,
                    sport=p.sport,
                    event_external_id=p.event_external_id,
                    event_name=p.event_name,
                    market=p.market,
                    selection=p.selection,
                    odds=Decimal(str(p.odds)),
                    meta=p.meta,
                )
            )
            latest = (
                await session.execute(
                    select(Price)
                    .where(
                        Price.provider == p.provider,
                        Price.book == p.book,
                        Price.event_external_id == p.event_external_id,
                        Price.market == p.market,
                        Price.selection == p.selection,
                    )
                    .order_by(Price.changed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            new_odds = Decimal(str(p.odds))
            if latest is None or latest.odds != new_odds:
                session.add(
                    Price(
                        changed_at=captured_at,
                        provider=p.provider,
                        book=p.book,
                        sport=p.sport,
                        event_external_id=p.event_external_id,
                        market=p.market,
                        selection=p.selection,
                        odds=new_odds,
                        prev_odds=None if latest is None else latest.odds,
                    )
                )
                changes += 1
        await session.commit()
    return {"snapshots": len(points), "price_changes": changes}


async def line_movement(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_external_id: str,
    market: str | None = None,
    selection: str | None = None,
    book: str | None = None,
) -> list[dict[str, Any]]:
    """The change-point series for an event, oldest first (the M2.1 exit-gate query)."""
    stmt = select(Price).where(Price.event_external_id == event_external_id)
    if market:
        stmt = stmt.where(Price.market == market)
    if selection:
        stmt = stmt.where(Price.selection == selection)
    if book:
        stmt = stmt.where(Price.book == book)
    stmt = stmt.order_by(Price.changed_at)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "book": r.book,
            "market": r.market,
            "selection": r.selection,
            "odds": float(r.odds),
            "prev_odds": float(r.prev_odds) if r.prev_odds is not None else None,
            "changed_at": r.changed_at.isoformat(),
        }
        for r in rows
    ]


async def prune_snapshots(
    session_factory: async_sessionmaker[AsyncSession], *, older_than_days: int = 90
) -> int:
    """Manual retention for non-Timescale deployments: raw snapshots beyond the window
    go; the change-point series in ``prices`` is the durable record and is kept."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
    async with session_factory() as session:
        result = await session.execute(delete(OddsSnapshot).where(OddsSnapshot.captured_at < cutoff))
        await session.commit()
    pruned = int(getattr(result, "rowcount", 0) or 0)
    if pruned:
        logger.info("pruned %d snapshots older than %dd", pruned, older_than_days)
    return pruned
