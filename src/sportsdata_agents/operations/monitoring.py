"""Line monitor (M3.2): standing watches over the ingestion stream → push alerts.

Deterministic, no LLM. Each ``Subscription`` is a durable watch with its own
``cursor`` — the engine scans only change-points written since the cursor, so a
missed cycle catches up instead of losing alerts (§8.2 durable/resumable). Kinds:

- ``line_move``  — a single change-point moved ≥ ``threshold_pct`` (params may
  filter sport/market/selection/book).
- ``steam``      — ≥ ``min_moves`` same-direction moves on one (event, market,
  selection) within ``window_minutes`` — the market walking somewhere.
- ``value``      — a recorded model prediction's edge at the latest price crossed
  ``min_edge_pct`` (appeared), or a previously-alerted edge dropped back under it
  (vanished).
- ``scratching`` — a racing selection whose prices stopped updating while the rest
  of its card moved on (scratching/suspension suspect).

Alerts dedupe on (subscription, dedupe_key): a persisting condition fires once,
not every cycle, and each watch fires at most ``max_alerts_per_cycle`` (default
10) per pass — the first pass over a deep backlog must not firehose the channel
(live lesson: Slack rate-limited the first unbounded run). Push: Slack
``chat.postMessage`` when the subscription names a channel and ``SLACK_BOT_TOKEN``
is set; otherwise the alert row + log line is the record.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Alert, OddsSnapshot, Prediction, Price, Subscription

logger = logging.getLogger(__name__)

Pusher = Callable[[Subscription, str], Awaitable[bool]]


async def slack_pusher(subscription: Subscription, message: str) -> bool:
    """Push to the subscription's Slack channel; False (logged) when unconfigured."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or subscription.channel in ("", "log"):
        logger.info("alert (log): %s", message)
        return False
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": subscription.channel, "text": message},
        )
    ok = bool(response.json().get("ok"))
    if not ok:
        logger.warning("slack push failed: %s", response.text[:200])
    return ok


def _pct_move(prev: float, new: float) -> float:
    return abs(new - prev) / prev * 100.0 if prev else 0.0


def _match(row: Price, params: dict[str, Any]) -> bool:
    for field in ("sport", "market", "selection", "book", "provider"):
        want = params.get(field)
        if want and getattr(row, field) != want:
            return False
    return True


async def _fire(
    session: AsyncSession,
    subscription: Subscription,
    *,
    kind: str,
    key: str,
    message: str,
    payload: dict[str, Any],
    pusher: Pusher,
) -> bool:
    """Write + push one alert unless the same condition already fired recently."""
    window = dt.timedelta(minutes=float(subscription.params.get("window_minutes", 60)))
    recent = (
        await session.execute(
            select(Alert)
            .where(
                Alert.subscription_id == subscription.id,
                Alert.dedupe_key == key,
                Alert.created_at >= dt.datetime.now(dt.UTC) - window,
            )
            .limit(1)
        )
    ).scalars().first()
    if recent is not None:
        return False
    try:
        pushed = await pusher(subscription, message)
    except Exception as e:  # a push failure must not sink the watch — the row is the record
        logger.warning("push failed for %s: %s", subscription.name, e)
        pushed = False
    session.add(Alert(
        tenant_id=subscription.tenant_id, workspace_id=subscription.workspace_id,
        subscription_id=subscription.id, kind=kind, message=message,
        payload=payload, dedupe_key=key, pushed=pushed,
    ))
    return True


async def _watch_line_move(
    session: AsyncSession, sub: Subscription, rows: list[Price], pusher: Pusher
) -> int:
    threshold = float(sub.params.get("threshold_pct", 5.0))
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    fired = 0
    for row in rows:
        if fired >= cap:
            break
        if row.prev_odds is None or not _match(row, sub.params):
            continue
        move = _pct_move(float(row.prev_odds), float(row.odds))
        if move < threshold:
            continue
        direction = "shortened" if float(row.odds) < float(row.prev_odds) else "drifted"
        message = (
            f":chart_with_upwards_trend: line move — {row.book} {row.market} "
            f"{row.selection!r} ({row.event_external_id}) {direction} "
            f"{float(row.prev_odds):.2f} → {float(row.odds):.2f} ({move:.1f}%)"
        )
        key = f"line_move:{row.book}:{row.event_external_id}:{row.market}:{row.selection}"
        if await _fire(session, sub, kind="line_move", key=key, message=message,
                       payload={"move_pct": round(move, 2), "odds": float(row.odds),
                                "prev_odds": float(row.prev_odds)}, pusher=pusher):
            fired += 1
    return fired


async def _watch_steam(
    session: AsyncSession, sub: Subscription, rows: list[Price], pusher: Pusher
) -> int:
    """min_moves same-direction change-points on one key inside the cursor window."""
    min_moves = int(sub.params.get("min_moves", 3))
    fired = 0
    by_key: dict[tuple[str, str, str, str], list[Price]] = {}
    for row in rows:
        if row.prev_odds is None or not _match(row, sub.params):
            continue
        by_key.setdefault((row.book, row.event_external_id, row.market, row.selection), []).append(row)
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    for (book, event, market, selection), series in by_key.items():
        if fired >= cap:
            break
        series.sort(key=lambda r: r.changed_at)
        directions = {1 if float(r.odds) > float(r.prev_odds or 0) else -1 for r in series}
        if len(series) >= min_moves and len(directions) == 1:
            arrow = "drifting" if directions == {1} else "steaming in"
            message = (
                f":fire: steam — {book} {market} {selection!r} ({event}) {arrow}: "
                f"{len(series)} consecutive moves, "
                f"{float(series[0].prev_odds or 0):.2f} → {float(series[-1].odds):.2f}"
            )
            key = f"steam:{book}:{event}:{market}:{selection}"
            if await _fire(session, sub, kind="steam", key=key, message=message,
                           payload={"moves": len(series)}, pusher=pusher):
                fired += 1
    return fired


async def _watch_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher
) -> int:
    """Model edge at the LATEST price crossing min_edge_pct — appear and vanish."""
    min_edge = float(sub.params.get("min_edge_pct", 3.0))
    fired = 0
    predictions = (
        await session.execute(
            select(Prediction).where(
                Prediction.tenant_id == sub.tenant_id,
                Prediction.workspace_id == sub.workspace_id,
            )
        )
    ).scalars().all()
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    for pred in predictions:
        if fired >= cap:
            break
        stmt = (
            select(Price)
            .where(
                Price.event_external_id == pred.event_external_id,
                Price.market == pred.market,
                Price.selection == pred.selection,
            )
            .order_by(Price.changed_at.desc())
            .limit(1)
        )
        latest = (await session.execute(stmt)).scalars().first()
        if latest is None or not _match(latest, sub.params):
            continue
        edge = (float(pred.prob) * float(latest.odds) - 1.0) * 100.0
        key = f"value:{pred.event_external_id}:{pred.market}:{pred.selection}"
        previously = (
            await session.execute(
                select(Alert)
                .where(Alert.subscription_id == sub.id, Alert.dedupe_key == key,
                       Alert.kind == "value")
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if edge >= min_edge:
            message = (
                f":moneybag: value — {latest.book} {pred.market} {pred.selection!r} "
                f"({pred.event_external_id}): model {float(pred.prob):.0%} at "
                f"{float(latest.odds):.2f} = +{edge:.1f}% edge"
            )
            if await _fire(session, sub, kind="value", key=key, message=message,
                           payload={"edge_pct": round(edge, 2)}, pusher=pusher):
                fired += 1
        elif previously is not None and previously.payload.get("edge_pct", 0) > 0:
            message = (
                f":hourglass: value gone — {pred.market} {pred.selection!r} "
                f"({pred.event_external_id}) edge now {edge:.1f}% (< {min_edge}%)"
            )
            if await _fire(session, sub, kind="value_vanished", key=f"vanished:{key}",
                           message=message, payload={"edge_pct": round(edge, 2)},
                           pusher=pusher):
                fired += 1
    return fired


async def _watch_scratching(
    session: AsyncSession, sub: Subscription, pusher: Pusher
) -> int:
    """A racing selection silent for stale_minutes while its card kept updating."""
    stale_minutes = float(sub.params.get("stale_minutes", 20))
    sport_like = str(sub.params.get("sport", "racing"))
    fired = 0
    rows = (
        await session.execute(
            select(
                OddsSnapshot.provider, OddsSnapshot.event_external_id,
                OddsSnapshot.selection,
                func.max(OddsSnapshot.captured_at),  # latest sighting per selection
            )
            .where(OddsSnapshot.sport.like(f"%{sport_like}%"),
                   OddsSnapshot.market == "win")
            .group_by(OddsSnapshot.provider, OddsSnapshot.event_external_id, OddsSnapshot.selection)
        )
    ).all()
    by_event: dict[tuple[str, str], list[tuple[str, dt.datetime]]] = {}
    for provider, event, selection, latest in rows:
        when = latest if isinstance(latest, dt.datetime) else dt.datetime.fromisoformat(str(latest))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        by_event.setdefault((provider, event), []).append((selection, when))
    gap = dt.timedelta(minutes=stale_minutes)
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    for (provider, event), entries in by_event.items():
        if fired >= cap:
            break
        if len(entries) < 3:
            continue
        freshest = max(when for _sel, when in entries)
        for selection, when in entries:
            if freshest - when >= gap:
                message = (
                    f":no_entry: scratching suspect — {provider} {event} runner "
                    f"{selection!r}: no prices since {when.isoformat()} while the "
                    f"card kept updating"
                )
                key = f"scratching:{provider}:{event}:{selection}"
                if await _fire(session, sub, kind="scratching", key=key, message=message,
                               payload={"last_seen": when.isoformat()}, pusher=pusher):
                    fired += 1
    return fired


async def run_watches(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pusher: Pusher | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """One monitoring pass over every active subscription. Durable: each watch's
    cursor advances only after its scan, so missed cycles replay."""
    push = pusher or slack_pusher
    now = now or dt.datetime.now(dt.UTC)
    report: dict[str, Any] = {"subscriptions": 0, "alerts": 0}
    async with session_factory() as session:
        subscriptions = (
            await session.execute(select(Subscription).where(Subscription.active.is_(True)))
        ).scalars().all()
        for sub in subscriptions:
            report["subscriptions"] += 1
            cursor = sub.cursor or now - dt.timedelta(hours=6)
            try:
                if sub.kind in ("line_move", "steam"):
                    rows = list(
                        (
                            await session.execute(
                                select(Price).where(Price.changed_at > cursor).order_by(Price.changed_at)
                            )
                        ).scalars()
                    )
                    if sub.kind == "line_move":
                        fired = await _watch_line_move(session, sub, rows, push)
                    else:
                        fired = await _watch_steam(session, sub, rows, push)
                elif sub.kind == "value":
                    fired = await _watch_value(session, sub, push)
                elif sub.kind == "scratching":
                    fired = await _watch_scratching(session, sub, push)
                else:
                    logger.warning("unknown watch kind %s (subscription %s)", sub.kind, sub.id)
                    continue
                sub.cursor = now
                report["alerts"] += fired
            except Exception as e:  # one broken watch must not sink the pass
                logger.warning("watch %s (%s) failed: %s", sub.name, sub.kind, e)
                report[f"error:{sub.name}"] = str(e)
        await session.commit()
    return report
