"""Warehouse writes + queries (M2.1): snapshots append, prices dedupe to change-points.

Dedupe contract: ``odds_snapshots`` records every observation (prunable);
``prices`` gains a row only when a key's odds MOVE (or on first sighting) — that is
the series line-movement queries and backtests read.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import OddsSnapshot, Price
from sportsdata_agents.operations.ingestion.normalizers import PricePoint

logger = logging.getLogger(__name__)

_DEDUPE_CHUNK = 500  # event ids per latest-price query (well under SQLite's var cap)

# The logical change-point key (unique index uq_prices_change, migration 0013). An ingest
# insert that collides on it is a re-run / same-timestamp race → DO NOTHING (idempotent).
_PRICE_UQ = ["provider", "book", "event_external_id", "market", "selection", "changed_at"]


def _price_insert(session: AsyncSession):
    """Dialect-aware INSERT for prices (postgres vs sqlite both support on_conflict)."""
    name = ""
    with contextlib.suppress(Exception):  # fall back to sqlite (dev) if bind is unset
        name = session.bind.dialect.name
    return _pg_insert(Price) if name == "postgresql" else _sqlite_insert(Price)


def _parse_start(value: Any) -> dt.datetime | None:
    """Provider start stamps → aware UTC datetime: ISO strings (any offset, Z, or
    naive-as-UTC) and epoch numbers (seconds or milliseconds — Sportsbet sends
    seconds). None for absent/unparseable: a null start just falls back to
    capture-day windowing in the resolver."""
    if value is None:
        return None
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        epoch = float(value)
        if epoch <= 0:
            return None
        if epoch > 1e12:  # milliseconds
            epoch /= 1000.0
        try:
            return dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None

_Key = tuple[str, str, str, str, str]


def _price_key(provider: str, book: str, event_id: str, market: str, selection: str) -> _Key:
    return (provider, book, event_id, market, selection)


async def _load_latest_odds(
    session: AsyncSession, points: list[PricePoint]
) -> dict[_Key, Decimal]:
    """The latest recorded odds for every key this batch touches — a handful of
    grouped queries instead of one SELECT per point (a 66K-snapshot cycle was
    issuing 66K lookups)."""
    providers = sorted({p.provider for p in points})
    event_ids = sorted({p.event_external_id for p in points})
    latest: dict[_Key, Decimal] = {}
    for start in range(0, len(event_ids), _DEDUPE_CHUNK):
        chunk = event_ids[start : start + _DEDUPE_CHUNK]
        sub = (
            select(
                Price.provider,
                Price.book,
                Price.event_external_id,
                Price.market,
                Price.selection,
                func.max(Price.changed_at).label("mx"),
            )
            .where(Price.provider.in_(providers), Price.event_external_id.in_(chunk))
            .group_by(
                Price.provider, Price.book, Price.event_external_id,
                Price.market, Price.selection,
            )
            .subquery()
        )
        rows = (
            await session.execute(
                select(Price).join(
                    sub,
                    and_(
                        Price.provider == sub.c.provider,
                        Price.book == sub.c.book,
                        Price.event_external_id == sub.c.event_external_id,
                        Price.market == sub.c.market,
                        Price.selection == sub.c.selection,
                        Price.changed_at == sub.c.mx,
                    ),
                )
            )
        ).scalars().all()
        for r in rows:
            latest[_price_key(r.provider, r.book, r.event_external_id, r.market, r.selection)] = r.odds
    return latest


async def record_points(
    session_factory: async_sessionmaker[AsyncSession],
    points: list[PricePoint],
    *,
    captured_at: dt.datetime | None = None,
) -> dict[str, int]:
    """Persist one capture: every point → a snapshot row; moved prices → change-points."""
    captured_at = captured_at or dt.datetime.now(dt.UTC)
    if not points:
        return {"snapshots": 0, "price_changes": 0}
    changes = 0
    async with session_factory() as session:
        latest = await _load_latest_odds(session, points)
        price_rows: list[dict[str, Any]] = []
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
                    start_time=_parse_start(p.meta.get("start_time") or p.meta.get("post_time")),
                    end_time=_parse_start(p.meta.get("end_time")),
                    meta=p.meta,
                )
            )
            key = _price_key(p.provider, p.book, p.event_external_id, p.market, p.selection)
            prev = latest.get(key)
            new_odds = Decimal(str(p.odds))
            if prev is None or prev != new_odds:
                price_rows.append({
                    "id": uuid.uuid4(),
                    "changed_at": captured_at,
                    "provider": p.provider,
                    "book": p.book,
                    "sport": p.sport,
                    "event_external_id": p.event_external_id,
                    "market": p.market,
                    "selection": p.selection,
                    "odds": new_odds,
                    "prev_odds": prev,
                })
                latest[key] = new_odds  # defensive: keys should be unique per batch
                changes += 1
        # ON CONFLICT DO NOTHING on the change-point unique index: a re-run or a
        # same-timestamp concurrent writer can't double-insert the same change-point.
        if price_rows:
            await session.execute(
                _price_insert(session).values(price_rows).on_conflict_do_nothing(index_elements=_PRICE_UQ)
            )
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
