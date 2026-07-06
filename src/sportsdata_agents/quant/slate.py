"""Slate recorder: price upcoming events through the engine and RECORD the
fair probabilities as predictions — the measurement half of the value loop.

The ``model_value`` watch prices boards inline for ALERTS, but persists
nothing, so there is nothing to grade later. This job walks events whose
calibration anchors moved recently (the same discovery the watch uses),
prices each board once through the configured engine, and stores the fair
probabilities under the auto-managed ``engine:{sport}`` model artifact —
the SAME rows ``run_backtest`` and the CLV report already consume. Each
(book, event) records at most once per ``dedupe_hours``, so the table
accrues a pre-game snapshot trail, not a copy per cron tick.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import ModelArtifact, Prediction, Price
from sportsdata_agents.data.repository import TenantScope

logger = logging.getLogger(__name__)

__all__ = ["SLATE_SPORTS", "record_slate"]

# engine sport -> warehouse price label; two rows where books label the same
# family differently (the dictionary's family keys predate the engine names)
SLATE_SPORTS: tuple[tuple[str, str], ...] = (
    ("afl", "australian_rules"),
    ("afl", "afl"),
    ("rugby_league", "rugby_league"),
    ("rugby_union", "rugby_union"),
    ("soccer", "soccer"),
    ("soccer", "football"),  # Kambi/Unibet's label for the same sport
    ("baseball", "baseball"),
    ("basketball", "basketball"),
    ("basketball", "nba"),
    ("basketball", "wnba"),
    ("nfl", "american_football"),
    ("cricket", "cricket"),
    ("ice_hockey", "ice_hockey"),
    # beyond the spine: sports whose anchors are NOT h2h+total pairs — each
    # gets its seed from _seed_for below
    ("tennis", "tennis"),
    ("darts", "darts"),
    ("snooker", "snooker"),
    ("mma", "mma"),
    ("mma", "ufc_mma"),
    ("mma", "martial_arts"),
    # racing: the win board is the anchor; recorded fair win/place probabilities
    # settle against racing results (selections are saddle numbers on both sides)
    ("racing", "horse_racing"),
    ("racing", "thoroughbred_racing"),  # RacingAndSports' label
    ("racing", "greyhound_racing"),
    ("racing", "harness_racing"),
    # golf: the outright field is the anchor (win probabilities per player)
    ("golf", "golf"),
    ("golf", "pga"),
)

# the golf outright market under its captured aliases
_GOLF_WIN_MARKETS = {"outright", "outright betting", "to win", "winner", "win only",
                     "win only img", "win"}

# sports whose engine seed is just the two-way h2h pair (no total required)
_H2H_ONLY_SPORTS = {"darts", "snooker", "mma"}


def _seed_for(sport: str, event_rows: list[Any]) -> dict[str, Any] | None:
    """The engine seed for one event's latest rows, in the sport's own anchor
    shape — None when the event isn't seedable (missing anchors)."""
    from sportsdata_agents.operations.monitoring import _footy_engine_inputs

    if sport == "racing":
        win_odds = {r.selection: float(r.odds) for r in event_rows
                    if r.market.lower() == "win"}
        return {"win_odds": win_odds} if len(win_odds) >= 2 else None
    if sport == "golf":
        win_odds = {r.selection: float(r.odds) for r in event_rows
                    if r.market.lower() in _GOLF_WIN_MARKETS}
        # a couple of quotes is a matchup, not a field — the engine wants the board
        return {"win_odds": win_odds} if len(win_odds) >= 5 else None
    seed, _ = _footy_engine_inputs(event_rows)
    if sport in _H2H_ONLY_SPORTS:
        # the contest/mma anchor is the h2h pair alone — totals (if the books
        # even quote them) are in different units per sport and not required
        h2h = seed.get("h2h") if seed else None
        if h2h is None:
            pair: dict[str, float] = {}
            for row in event_rows:
                side = row.selection.lower()
                if row.market.lower() in ("h2h", "2way", "head_to_head", "match_winner") \
                        and side in ("home", "away"):
                    pair[side] = float(row.odds)
            h2h = [pair["home"], pair["away"]] if len(pair) == 2 else None
        return {"h2h": h2h} if h2h else None
    if seed is not None and sport == "tennis":
        # tennis totals are GAMES — the engine's anchor key says so
        seed = {"h2h": seed["h2h"], "total_games": seed["total"]}
    return seed


async def record_slate(
    session: AsyncSession,
    scope: TenantScope,
    *,
    now: dt.datetime,
    sports: tuple[tuple[str, str], ...] = SLATE_SPORTS,
    anchor_minutes: float = 45.0,
    dedupe_hours: float = 12.0,
    max_events: int = 40,
) -> dict[str, Any]:
    """Record engine fair probabilities for every scannable (book, event).

    Returns {"recorded", "events", "skipped_dedupe", "skipped_unseedable"}.
    Degrades cleanly: no engine configured -> nothing recorded, said so.
    """
    from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine
    from sportsdata_agents.tools.quant import _warehouse_key

    try:
        engine = resolve_engine()
    except (EngineUnavailable, ValueError) as e:
        return {"recorded": 0, "events": 0, "error": str(e)}
    if engine is None:
        return {"recorded": 0, "events": 0, "error": "no pricing engine configured"}

    # mirror the model_value watch's freshness model: the warehouse stores
    # CHANGE-POINTS, so the latest row per key is the current quote however
    # old — load a ttl window, then GATE each event on recent anchor movement.
    #
    # READS FIRST, WRITES IN SHORT TRANSACTIONS: on live SQLite the ingest
    # cron advances the WAL constantly; a session that reads for seconds and
    # then INSERTs upgrades a stale snapshot and gets an IMMEDIATE "database
    # is locked" (no busy-wait applies — lived on the first live run). So the
    # discovery reads are committed away before any write, and each event's
    # predictions commit in their own small transaction.
    anchor_cutoff = now - dt.timedelta(minutes=anchor_minutes)
    ttl_cutoff = now - dt.timedelta(hours=24.0)
    anchor_markets = {"2way", "h2h", "head_to_head", "match_winner", "total", "totals",
                      "win", *_GOLF_WIN_MARKETS}  # racing/golf anchor on the win board
    recorded = 0
    events_priced = 0
    skipped_dedupe = 0
    skipped_unseedable = 0
    candidates: list[tuple[str, str, str, dict[str, Any]]] = []  # (sport, book, event, seed)
    for sport, price_sport in sports:
        rows = (await session.execute(
            select(Price).where(Price.sport == price_sport, Price.changed_at > ttl_cutoff)
            .order_by(Price.changed_at.desc())
        )).scalars().all()
        latest: dict[tuple[str, str, str, str], Price] = {}
        for row in rows:
            latest.setdefault((row.book, row.event_external_id, row.market, row.selection), row)
        by_event: dict[tuple[str, str], list[Price]] = {}
        for (book, event_id, _, _), row in latest.items():
            by_event.setdefault((book, event_id), []).append(row)
        for (book, event_id), event_rows in sorted(by_event.items()):
            if not any(
                (r.changed_at if r.changed_at.tzinfo else r.changed_at.replace(tzinfo=dt.UTC))
                > anchor_cutoff
                for r in event_rows if r.market.lower() in anchor_markets
            ):
                continue  # nothing moved — the previous snapshot still stands
            seed = _seed_for(sport, event_rows)
            if seed is None:
                skipped_unseedable += 1
                continue
            candidates.append((sport, book, event_id, seed))
    await session.commit()  # end the read snapshot BEFORE any write

    # ESOCCER GUARD: Kambi's "football" label carries FIFA video-game matches
    # ("River Plate (Galikooo) - Boca Juniors (drksd3)") — both sides tagged
    # with a gamertag. The soccer model must not price video games. Women's
    # "(W)" and similar one-letter tags are real fixtures and stay.
    import re as _re

    if candidates:
        from sportsdata_agents.data.models import OddsSnapshot

        ids = sorted({event_id for _s, _b, event_id, _seed in candidates})
        names: dict[str, str] = {
            str(event_id): str(name) for event_id, name in (await session.execute(
                select(OddsSnapshot.event_external_id, OddsSnapshot.event_name)
                .where(OddsSnapshot.event_external_id.in_(ids)).distinct()
            )).all()}

        def _is_esports(name: str) -> bool:
            tags = _re.findall(r"\(([^)\s]{2,20})\)", name or "")
            return len(tags) >= 2

        kept = [c for c in candidates if not _is_esports(names.get(c[2], ""))]
        if len(kept) < len(candidates):
            logger.info("slate: %d gamertag-styled events skipped (esports)",
                        len(candidates) - len(kept))
        candidates = kept

    # cache the artifact IDS (plain uuids), never the ORM objects: a lock
    # retry rolls the session back, which expires loaded attributes — touching
    # an expired object then triggers sync IO inside the async loop (xd2s)
    artifacts: dict[str, Any] = {}
    for sport, book, event_id, seed in candidates:
        if events_priced >= max_events:
            break

        async def _write_event(sport: str = sport, book: str = book,
                               event_id: str = event_id, seed: dict[str, Any] = seed) -> int:
            if sport not in artifacts:
                created = await _engine_artifact(session, scope, sport, type(engine).__name__)
                artifact_id = created.id
                await session.commit()
                artifacts[sport] = artifact_id
            fresh = (await session.execute(
                select(Prediction.id).where(
                    Prediction.tenant_id == scope.tenant_id,
                    Prediction.workspace_id == scope.workspace_id,
                    Prediction.model_id == artifacts[sport],
                    Prediction.provider == book,
                    Prediction.event_external_id == event_id,
                    Prediction.predicted_at > now - dt.timedelta(hours=dedupe_hours),
                ).limit(1)
            )).scalar_one_or_none()
            if fresh is not None:
                return -1  # deduped
            # price AFTER the dedupe check (deduped events cost nothing);
            # a lock-retry re-prices against the engines' in-process cache
            board = engine.price_board(sport, event_id, seed)
            added = 0
            for price in board:
                if not 0.0 < price.fair_probability < 1.0:
                    continue  # degenerate corners are not predictions
                key = _warehouse_key(price.market, price.selection, price.line)
                if key is None:
                    continue  # no stable warehouse convention for this family yet
                market, selection = key
                session.add(Prediction(
                    tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                    model_id=artifacts[sport], provider=book,
                    event_external_id=event_id, market=market, selection=selection,
                    prob=Decimal(str(round(price.fair_probability, 5))),
                    predicted_at=now,
                ))
                added += 1
            await session.commit()  # one small write transaction per event
            return added

        try:
            added = await _retry_locked(session, _write_event, label=f"{sport}/{event_id}")
        except (EngineUnavailable, ValueError) as e:
            # one hostile event must not sink the slate
            logger.info("slate: could not price %s/%s: %s", sport, event_id, e)
            continue
        if added is None:
            continue  # gave up on a persistently locked write; the rest proceed
        if added < 0:
            skipped_dedupe += 1
            continue
        events_priced += 1
        recorded += added
    return {"recorded": recorded, "events": events_priced,
            "skipped_dedupe": skipped_dedupe, "skipped_unseedable": skipped_unseedable}


async def _retry_locked(session: AsyncSession, unit: Any, *, label: str,
                        attempts: int = 5) -> int | None:
    """Run one small write unit, retrying SQLite lock upgrades.

    On a live single-writer warehouse the ingest cron advances the WAL
    between our read and our write, which surfaces as an IMMEDIATE
    "database is locked" (busy_timeout does not apply to snapshot
    upgrades). The unit re-reads and re-stages everything each attempt,
    so a rollback loses nothing."""
    from sqlalchemy.exc import OperationalError

    for attempt in range(attempts):
        try:
            return int(await unit())
        except OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            await session.rollback()
            await asyncio.sleep(1.5 * (attempt + 1))
    logger.warning("slate: %s still locked after %d attempts — skipped", label, attempts)
    return None


async def _engine_artifact(
    session: AsyncSession, scope: TenantScope, sport: str, backend: str
) -> ModelArtifact:
    """The auto-managed engine:{sport} artifact (same convention as the
    engine_fair_prices tool, so backtest/CLV see ONE model per sport)."""
    name = f"engine:{sport}"
    artifact = (await session.execute(
        select(ModelArtifact).where(
            ModelArtifact.tenant_id == scope.tenant_id,
            ModelArtifact.workspace_id == scope.workspace_id,
            ModelArtifact.name == name,
        ).order_by(ModelArtifact.version.desc()).limit(1)
    )).scalar_one_or_none()
    if artifact is None:
        artifact = ModelArtifact(
            tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
            name=name, version=1, sport=sport, market="board",
            params={"backend": backend, "source": "price-slate"},
            calibration={"source": "pricing-engine", "measured_by": "replay"},
            trained_at=dt.datetime.now(dt.UTC),
        )
        session.add(artifact)
        await session.flush()
    return artifact
