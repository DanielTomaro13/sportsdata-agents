"""Warehouse writes + queries (M2.1): snapshots dedupe in place, prices dedupe to change-points.

Dedupe contract: ``odds_snapshots`` keeps the LATEST observation per
(provider, book, event, market, selection) price level — an unchanged capture
refreshes that row's ``captured_at`` and ``meta`` instead of appending (81% of
raw captures carried odds identical to the previous cycle; append-only burned
2.4GB/day writing them). Every scan that reads ``captured_at`` as "when was
this price last confirmed live" (racing staleness, arb max-age, scratching)
keeps exactly its semantics. The full history of MOVES lives in ``prices``:
a change-point row per odds change (or first sighting) — that is the series
line-movement queries, models and backtests read, and it is never pruned.
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


async def _load_latest_snapshots(
    session: AsyncSession, points: list[PricePoint]
) -> dict[_Key, tuple[Any, Decimal]]:
    """The latest snapshot row's (id, odds) for every key this batch touches —
    the in-place-refresh targets. Grouped like _load_latest_odds: a handful of
    chunked queries, never one per point."""
    providers = sorted({p.provider for p in points})
    event_ids = sorted({p.event_external_id for p in points})
    latest: dict[_Key, tuple[Any, Decimal]] = {}
    for start in range(0, len(event_ids), _DEDUPE_CHUNK):
        chunk = event_ids[start : start + _DEDUPE_CHUNK]
        sub = (
            select(
                OddsSnapshot.provider,
                OddsSnapshot.book,
                OddsSnapshot.event_external_id,
                OddsSnapshot.market,
                OddsSnapshot.selection,
                func.max(OddsSnapshot.captured_at).label("mx"),
            )
            .where(OddsSnapshot.provider.in_(providers),
                   OddsSnapshot.event_external_id.in_(chunk))
            .group_by(
                OddsSnapshot.provider, OddsSnapshot.book, OddsSnapshot.event_external_id,
                OddsSnapshot.market, OddsSnapshot.selection,
            )
            .subquery()
        )
        rows = (
            await session.execute(
                select(OddsSnapshot.id, OddsSnapshot.provider, OddsSnapshot.book,
                       OddsSnapshot.event_external_id, OddsSnapshot.market,
                       OddsSnapshot.selection, OddsSnapshot.odds).join(
                    sub,
                    and_(
                        OddsSnapshot.provider == sub.c.provider,
                        OddsSnapshot.book == sub.c.book,
                        OddsSnapshot.event_external_id == sub.c.event_external_id,
                        OddsSnapshot.market == sub.c.market,
                        OddsSnapshot.selection == sub.c.selection,
                        OddsSnapshot.captured_at == sub.c.mx,
                    ),
                )
            )
        ).all()
        for r in rows:
            latest[_price_key(r.provider, r.book, r.event_external_id, r.market, r.selection)] = (
                r.id, r.odds)
    return latest


async def record_points(
    session_factory: async_sessionmaker[AsyncSession],
    points: list[PricePoint],
    *,
    captured_at: dt.datetime | None = None,
) -> dict[str, int]:
    """Persist one capture: moved/new prices insert a snapshot + a change-point;
    an unchanged price refreshes its existing snapshot row in place (captured_at
    + meta), so the raw table holds one row per price LEVEL, not per cycle.

    SPORTSDATA_AGENTS_SKIP_MARKETS (csv of market keys, e.g. "forecast,quinella,
    first_four") drops those markets at the door — the operator's storage dial
    for exotics the scans never read. Empty (default) stores everything.
    SPORTSDATA_AGENTS_SNAPSHOT_APPEND=1 restores the old append-every-observation
    behaviour (the rollback lever)."""
    import os

    captured_at = captured_at or dt.datetime.now(dt.UTC)
    skip = {m.strip().lower() for m in
            os.environ.get("SPORTSDATA_AGENTS_SKIP_MARKETS", "").split(",") if m.strip()}
    if skip:
        points = [p for p in points if p.market.lower() not in skip]
    if not points:
        return {"snapshots": 0, "price_changes": 0, "refreshed": 0}
    append_mode = os.environ.get("SPORTSDATA_AGENTS_SNAPSHOT_APPEND", "") == "1"
    changes = 0
    async with session_factory() as session:
        latest = await _load_latest_odds(session, points)
        snap_targets = ({} if append_mode
                        else await _load_latest_snapshots(session, points))
        price_rows: list[dict[str, Any]] = []
        refresh_rows: list[dict[str, Any]] = []
        for p in points:
            key = _price_key(p.provider, p.book, p.event_external_id, p.market, p.selection)
            new_odds = Decimal(str(p.odds))
            target = snap_targets.get(key)
            if target is not None and target[1] == new_odds:
                # same price as the standing row: refresh its freshness stamp and
                # meta (traded volume etc. move even when the odds don't) — every
                # staleness gate reads captured_at as "last confirmed live"
                refresh_rows.append({"b_id": target[0], "captured_at": captured_at,
                                     "meta": p.meta})
            else:
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
                        odds=new_odds,
                        start_time=_parse_start(p.meta.get("start_time") or p.meta.get("post_time")),
                        end_time=_parse_start(p.meta.get("end_time")),
                        meta=p.meta,
                    )
                )
                if not append_mode:
                    # a later duplicate of this key in the SAME batch must refresh,
                    # not insert twice (id unknown yet: None marks "just inserted")
                    snap_targets[key] = (None, new_odds)
            prev = latest.get(key)
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
        refresh_rows = [r for r in refresh_rows if r["b_id"] is not None]
        if refresh_rows:
            from sqlalchemy import bindparam, update

            # core-table update: an executemany refresh, not an ORM bulk update
            # (the ORM path insists on a session-synchronization strategy)
            table: Any = OddsSnapshot.__table__
            await session.execute(
                update(table)
                .where(table.c.id == bindparam("b_id"))
                .values(captured_at=bindparam("captured_at"), meta=bindparam("meta")),
                refresh_rows,
            )
        # ON CONFLICT DO NOTHING on the change-point unique index: a re-run or a
        # same-timestamp concurrent writer can't double-insert the same change-point.
        # CHUNKED: asyncpg caps one statement at 32,767 bind params (10 per row) —
        # a full-book feed's first pass ships enough change-points to blow it.
        for start in range(0, len(price_rows), 3000):
            chunk = price_rows[start:start + 3000]
            await session.execute(
                _price_insert(session).values(chunk).on_conflict_do_nothing(index_elements=_PRICE_UQ)
            )
        await session.commit()
    return {"snapshots": len(points), "price_changes": changes, "refreshed": len(refresh_rows)}


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


PRUNE_BATCH_ROWS = 200_000
PRUNE_MAX_BATCHES = 25  # per pass — the hourly custodian finishes the tail


async def prune_snapshots(
    session_factory: async_sessionmaker[AsyncSession], *, older_than_days: int = 90
) -> int:
    """Manual retention for non-Timescale deployments: raw snapshots beyond the window
    go; the change-point series in ``prices`` is the durable record and is kept.

    Deletes in BATCHES, one committed transaction each: a single-transaction
    delete of tens of millions of rows held the warehouse's only writer for
    30+ CPU-minutes and ballooned the WAL past 8GB while alerting starved
    (lived). A bounded pass leaves the tail for the next custodian run."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
    pruned = 0
    for _ in range(PRUNE_MAX_BATCHES):
        async with session_factory() as session:
            batch_ids = select(OddsSnapshot.id).where(
                OddsSnapshot.captured_at < cutoff
            ).limit(PRUNE_BATCH_ROWS).scalar_subquery()
            result = await session.execute(
                delete(OddsSnapshot).where(OddsSnapshot.id.in_(batch_ids)))
            await session.commit()
        got = int(getattr(result, "rowcount", 0) or 0)
        pruned += got
        if got < PRUNE_BATCH_ROWS:
            break  # window clear
    if pruned:
        logger.info("pruned %d snapshots older than %dd", pruned, older_than_days)
    return pruned
