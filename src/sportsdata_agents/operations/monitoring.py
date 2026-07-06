"""Line monitor (M3.2): standing watches over the ingestion stream → push alerts.

Deterministic, no LLM. Each ``Subscription`` is a durable watch with its own
``cursor`` — the engine scans only change-points written since the cursor, so a
missed cycle catches up instead of losing alerts (§8.2 durable/resumable). Kinds:

- ``arb``        — a complete cross-book outcome board whose best prices sum
  under 1 by ≥ ``threshold_pct`` gross margin (quant.arbitrage does the math).
- ``line_move``  — a single change-point moved ≥ ``threshold_pct`` (params may
  filter sport/market/selection/book).
- ``steam``      — ≥ ``min_moves`` same-direction moves on one (event, market,
  selection) within ``window_minutes`` — the market walking somewhere.
- ``value``      — a recorded model prediction's edge at the latest price crossed
  ``min_edge_pct`` (appeared), or a previously-alerted edge dropped back under it
  (vanished).
- ``scratching`` — a racing selection whose prices stopped updating while the rest
  of its card moved on (scratching/suspension suspect).
- ``model_value`` — a pricing engine (optional; quant.engines) calibrated to a
  book's own anchors disagrees with that book's derivative quotes by more than
  the noise band (consistency edge). Skips cleanly when no engine is configured.

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
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import (
    Alert,
    Event,
    OddsSnapshot,
    Prediction,
    Price,
    Subscription,
)

logger = logging.getLogger(__name__)

Pusher = Callable[[Subscription, str], Awaitable[bool]]


async def slack_pusher(subscription: Subscription, message: str) -> bool:
    """Route the alert to the subscription's channel — a Slack channel id,
    "discord[:ENV_VAR]" for a webhook, "ntfy[:ENV_VAR]" for phone push, or
    "log". (The name predates the platform parity; this is the router.)"""
    from sportsdata_agents.observability.notify import push_to_channel

    return await push_to_channel(subscription.channel or "", message)


def _pct_move(prev: float, new: float) -> float:
    return abs(new - prev) / prev * 100.0 if prev else 0.0


async def _cross_book_line(session: AsyncSession, row: Price) -> str:
    """The same market at every OTHER book mapped to this event's fixture —
    best price first. Side-relative selections (home/away) translate between
    books' listing orders with the settlement-grade name matching; when the
    event isn't resolved onto a fixture yet, the line is simply omitted."""
    from sportsdata_agents.quant.backtest import _event_name_for, _translate_side

    mapping = (
        await session.execute(
            select(Event).where(
                Event.provider == row.provider,
                Event.external_id == row.event_external_id,
            )
        )
    ).scalars().first()
    if mapping is None or mapping.fixture_id is None:
        return ""
    siblings = (
        await session.execute(
            select(Event).where(
                Event.fixture_id == mapping.fixture_id,
                Event.id != mapping.id,
            )
        )
    ).scalars().all()
    if not siblings:
        return ""
    side_relative = row.selection in ("home", "away", "draw")
    name_cache: dict[tuple[str, str], str] = {}
    own_name = ""
    if side_relative:
        own_name = await _event_name_for(session, name_cache, row.provider, row.event_external_id)
    quotes: dict[str, float] = {}
    for sibling in siblings:
        selection = row.selection
        if side_relative:
            sibling_name = await _event_name_for(session, name_cache, sibling.provider, sibling.external_id)
            translated = _translate_side(row.selection, sibling_name, own_name)
            if translated is None:
                continue  # orientation unknown — never show a possibly-flipped price
            selection = translated
        latest = (
            await session.execute(
                select(Price)
                .where(
                    Price.provider == sibling.provider,
                    Price.event_external_id == sibling.external_id,
                    Price.market == row.market,
                    Price.selection == selection,
                )
                .order_by(Price.changed_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is not None and latest.book != row.book:
            quotes[latest.book] = max(quotes.get(latest.book, 0.0), float(latest.odds))
    if not quotes:
        return ""
    board = " · ".join(f"{book} {odds:.2f}"
                       for book, odds in sorted(quotes.items(), key=lambda kv: -kv[1])[:6])
    return f"\nacross books: {board}"


async def _context(session: AsyncSession, row: Price) -> dict[str, str]:
    """Human context for an alert — the change-point series carries only keys;
    the event NAME, sport and runner/team label live in the latest snapshot."""
    snap = (
        await session.execute(
            select(OddsSnapshot)
            .where(
                OddsSnapshot.provider == row.provider,
                OddsSnapshot.event_external_id == row.event_external_id,
                OddsSnapshot.market == row.market,
                OddsSnapshot.selection == row.selection,
            )
            .order_by(OddsSnapshot.captured_at.desc())
            .limit(1)
        )
    ).scalars().first()
    event = (snap.event_name if snap else "") or row.event_external_id
    sport = (snap.sport if snap else "") or row.sport
    who = ((snap.meta or {}).get("runner") or (snap.meta or {}).get("team")) if snap else None
    selection = row.selection
    if who and str(who).strip().lower() != row.selection.lower():
        selection = f"{row.selection} ({who})"
    return {"event": event, "sport": sport, "selection": selection}


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
    if float(subscription.params.get("digest_hours", 0) or 0) > 0:
        pushed = False  # digest watches batch their pushes (see _push_digest)
    else:
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
        ctx = await _context(session, row)
        message = (
            f":chart_with_upwards_trend: line move [{ctx['sport']}] {ctx['event']}\n"
            f"{row.book} · {row.market} · {ctx['selection']} — "
            f"{float(row.prev_odds):.2f} → {float(row.odds):.2f} ({direction} {move:.1f}%)"
            + await _cross_book_line(session, row)
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
            ctx = await _context(session, series[-1])
            message = (
                f":fire: steam [{ctx['sport']}] {ctx['event']}\n"
                f"{book} · {market} · {ctx['selection']} — {arrow}, "
                f"{len(series)} consecutive moves, "
                f"{float(series[0].prev_odds or 0):.2f} → {float(series[-1].odds):.2f}"
                + await _cross_book_line(session, series[-1])
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
        # Band the dedupe key by edge magnitude (like the arb watch) so a materially BIGGER
        # edge re-fires instead of being swallowed by the stable-key dedupe window. The
        # `previously` lookup matches ANY band of this selection (startswith, autoescaped so
        # ids containing `_`/`%` don't act as LIKE wildcards) so "value gone" still works.
        base_key = f"value:{pred.event_external_id}:{pred.market}:{pred.selection}"
        band = int(max(edge, 0.0) / 2.0)  # re-fire each +2% the edge grows
        key = f"{base_key}:{band}"
        previously = (
            await session.execute(
                select(Alert)
                .where(Alert.subscription_id == sub.id,
                       Alert.dedupe_key.startswith(f"{base_key}:", autoescape=True),
                       Alert.kind == "value")
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if edge >= min_edge:
            ctx = await _context(session, latest)
            message = (
                f":moneybag: value [{ctx['sport']}] {ctx['event']}\n"
                f"{latest.book} · {pred.market} · {ctx['selection']} — model "
                f"{float(pred.prob):.0%} at {float(latest.odds):.2f} = +{edge:.1f}% edge"
                + await _cross_book_line(session, latest)
            )
            payload = {
                "edge_pct": round(edge, 2), "prob": float(pred.prob),
                "min_edge_pct": min_edge, "provider": latest.provider,
                "book": latest.book, "event_external_id": pred.event_external_id,
                "market": pred.market, "selection": pred.selection,
            }
            if await _fire(session, sub, kind="value", key=key, message=message,
                           payload=payload, pusher=pusher):
                fired += 1
        elif previously is not None and previously.payload.get("edge_pct", 0) > 0:
            message = (
                f":hourglass: value gone — {pred.market} {pred.selection!r} "
                f"({pred.event_external_id}) edge now {edge:.1f}% (< {min_edge}%)"
            )
            if await _fire(session, sub, kind="value_vanished", key=f"vanished:{base_key}",
                           message=message, payload={"edge_pct": round(edge, 2)},
                           pusher=pusher):
                fired += 1
    return fired


def _split_selection(selection: str) -> tuple[str, float | None]:
    """Normalised selections embed lines as a trailing number: ``home -1.5``,
    ``over 220.5`` → (side, line); plain sides/runners come back line-less."""
    head, _, tail = selection.rpartition(" ")
    if head:
        try:
            return head, float(tail)
        except ValueError:
            pass
    return selection, None


_H2H_MARKETS = {"2way", "h2h", "head_to_head", "match_winner"}
_TOTAL_MARKETS = {"total", "totals"}
_LINE_MARKETS = {"spread", "line", "handicap"}


def _footy_engine_inputs(rows: list[Price]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(engine seed quotes, derivative book quotes) from one event's latest rows.

    Seeds need a two-way h2h pair and the most balanced total line with both
    sides quoted; every parseable row also becomes a derivative quote (the
    anchors re-appear there harmlessly — calibration pins their edge to ~0).
    """
    h2h: dict[str, float] = {}
    totals: dict[float, dict[str, float]] = {}
    book_quotes: list[dict[str, Any]] = []
    for row in rows:
        market = row.market.lower()
        side, line = _split_selection(row.selection.lower())
        odds = float(row.odds)
        if market in _H2H_MARKETS and side in ("home", "away"):
            h2h[side] = odds
            book_quotes.append({"market": "h2h", "selection": side, "line": None, "odds": odds})
        elif market in _TOTAL_MARKETS and side in ("over", "under") and line is not None:
            totals.setdefault(line, {})[side] = odds
            book_quotes.append({"market": "total", "selection": side, "line": line, "odds": odds})
        elif market in _LINE_MARKETS and side in ("home", "away") and line is not None:
            book_quotes.append({"market": "line", "selection": side, "line": line, "odds": odds})
    paired = {ln: p for ln, p in totals.items() if len(p) == 2}
    if len(h2h) != 2 or not paired:
        return None, []
    main = min(paired, key=lambda ln: abs(1.0 / paired[ln]["over"] - 1.0 / paired[ln]["under"]))
    seed = {
        "h2h": [h2h["home"], h2h["away"]],
        "total": [main, paired[main]["over"], paired[main]["under"]],
    }
    return seed, book_quotes


def _racing_engine_inputs(
    rows: list[Price], places: int | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Racing: the win board seeds the engine; place quotes are the derivatives."""
    win_odds: dict[str, float] = {}
    place_rows: list[tuple[str, float]] = []
    for row in rows:
        market = row.market.lower()
        if market == "win":
            win_odds[row.selection] = float(row.odds)
        elif market == "place":
            place_rows.append((row.selection, float(row.odds)))
    if len(win_odds) < 2 or places is None:
        # paid place terms are the BOOK's, not derivable from field size (promos,
        # terms fixed pre-scratching) — without params.places we refuse to guess:
        # a wrong line makes every comparison a phantom edge
        return None, []
    book_quotes = [
        {"market": "place", "selection": runner, "line": float(places), "odds": odds}
        for runner, odds in place_rows
    ]
    return {"win_odds": win_odds}, book_quotes


async def _watch_model_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime
) -> int:
    """Engine fair prices vs a book's own derivative quotes (consistency edge).

    Noise-aware: candidates must clear ``min_edge_pct`` AND ``error_multiple``
    Monte Carlo standard errors (quant.engine_value does the math). Freshness
    is ANCHOR-gated: the warehouse stores change-points, so the latest row per
    key is the current quote regardless of age — an event is scanned when its
    calibration anchors (h2h/total; racing: win) moved within
    ``max_age_minutes``, and every current derivative quote is then compared,
    including ones that have NOT moved since (the laggy derivative is the
    consistency edge). Rows older than ``derivative_ttl_hours`` (default 24)
    are treated as likely-suspended markets and dropped. Degrades cleanly:
    with no engine configured the watch logs and fires nothing.
    """
    from sportsdata_agents.quant.engine_value import consistency_scan
    from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine

    try:
        engine = resolve_engine()
    except (EngineUnavailable, ValueError) as e:
        logger.info("model_value watch %s: engine unavailable (%s)", sub.name, e)
        return 0
    if engine is None:
        logger.info("model_value watch %s: no engine configured — skipping", sub.name)
        return 0

    sport = str(sub.params.get("sport", ""))
    if not sport:
        raise ValueError("model_value watch needs params.sport (an engine sport, e.g. afl|racing)")
    price_sport = str(sub.params.get("price_sport", sport))  # warehouse sport label if it differs
    book = sub.params.get("book")
    min_edge = float(sub.params.get("min_edge_pct", 3.0))
    error_multiple = float(sub.params.get("error_multiple", 3.0))
    max_age = dt.timedelta(minutes=float(sub.params.get("max_age_minutes", 30.0)))
    derivative_ttl = dt.timedelta(hours=float(sub.params.get("derivative_ttl_hours", 24.0)))
    cap = int(sub.params.get("max_alerts_per_cycle", 10))

    stmt = select(Price).where(Price.sport == price_sport, Price.changed_at > now - derivative_ttl)
    if book:
        stmt = stmt.where(Price.book == str(book))
    rows = (await session.execute(stmt.order_by(Price.changed_at.desc()))).scalars().all()
    latest: dict[tuple[str, str, str, str], Price] = {}
    for row in rows:
        latest.setdefault((row.book, row.event_external_id, row.market, row.selection), row)
    by_event: dict[tuple[str, str], list[Price]] = {}
    for (row_book, event_id, _, _), row in latest.items():
        by_event.setdefault((row_book, event_id), []).append(row)
    anchor_markets = {"win"} if sport == "racing" else (_H2H_MARKETS | _TOTAL_MARKETS)

    fired = 0
    for (row_book, event_id), event_rows in sorted(by_event.items()):
        if fired >= cap:
            break
        # anchor gate: scan only where the calibration inputs moved recently —
        # the derivatives themselves may be arbitrarily old change-points
        # (unchanged quote = current quote), which is exactly what we compare
        cutoff = now - max_age
        if not any(
            (r.changed_at if r.changed_at.tzinfo else r.changed_at.replace(tzinfo=dt.UTC)) > cutoff
            for r in event_rows if r.market.lower() in anchor_markets
        ):
            continue
        if sport == "racing":
            places = sub.params.get("places")
            seed, book_quotes = _racing_engine_inputs(event_rows, int(places) if places else None)
            if seed is None and len(event_rows) >= 2 and places is None:
                logger.info("model_value %s: racing needs params.places (the book's paid "
                            "place terms) — skipping %s", sub.name, event_id)
        else:
            seed, book_quotes = _footy_engine_inputs(event_rows)
        if seed is None or not book_quotes:
            continue
        try:
            board = engine.price_board(sport, event_id, seed)
            engine_rows = [
                {"market": p.market, "selection": p.selection, "line": p.line,
                 "fair_probability": p.fair_probability, "std_error": p.std_error}
                for p in board
            ]
            scan = consistency_scan(book_quotes, engine_rows, min_edge_pct=min_edge, error_multiple=error_multiple)
        except (EngineUnavailable, ValueError) as e:
            # one hostile event (bad odds row, unpriceable seed) must not kill
            # the rest of the subscription's cycle
            logger.info("model_value: could not evaluate %s: %s", event_id, e)
            continue
        for candidate in scan["candidates"]:
            if fired >= cap:
                break
            band = int(candidate["edge_pct"] / 2.0)
            base = (
                f"model_value:{row_book}:{event_id}:{candidate['market']}"
                f":{candidate['selection']}:{candidate['line']}"
            )
            at_line = f" @ {candidate['line']}" if candidate["line"] is not None else ""
            display = await _event_display_name(session, event_id) or event_id
            message = (
                f":crystal_ball: model value [{sport}] {display}\n"
                f"{row_book} · {candidate['market']} · {candidate['selection']}{at_line} — "
                f"book {candidate['odds']:.2f} vs model fair {candidate['model_fair_odds']} "
                f"(+{candidate['edge_pct']:.1f}% edge)"
            )
            payload = {**candidate, "book": row_book, "event_external_id": event_id,
                       "sport": sport, "noise_gated": scan["skipped_noise"]}
            if await _fire(session, sub, kind="model_value", key=f"{base}:{band}",
                           message=message, payload=payload, pusher=pusher):
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
                func.max(OddsSnapshot.event_name),
                func.max(OddsSnapshot.sport),
            )
            .where(OddsSnapshot.sport.like(f"%{sport_like}%"),
                   OddsSnapshot.market == "win")
            .group_by(OddsSnapshot.provider, OddsSnapshot.event_external_id, OddsSnapshot.selection)
        )
    ).all()
    by_event: dict[tuple[str, str], list[tuple[str, dt.datetime]]] = {}
    names: dict[tuple[str, str], tuple[str, str]] = {}
    for provider, event, selection, latest, event_name, sport in rows:
        when = latest if isinstance(latest, dt.datetime) else dt.datetime.fromisoformat(str(latest))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        by_event.setdefault((provider, event), []).append((selection, when))
        names[(provider, event)] = (str(event_name or event), str(sport or sport_like))
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
                event_name, sport = names.get((provider, event), (event, sport_like))
                message = (
                    f":no_entry: scratching suspect [{sport}] {event_name}\n"
                    f"{provider} · runner {selection} — no prices since "
                    f"{when.strftime('%H:%M UTC')} while the card kept updating"
                )
                key = f"scratching:{provider}:{event}:{selection}"
                if await _fire(session, sub, kind="scratching", key=key, message=message,
                               payload={"last_seen": when.isoformat()}, pusher=pusher):
                    fired += 1
    return fired


def _fmt_money(amount: float | None) -> str:
    """Traded-volume display for alerts: $16, $12.3k, $1.2M."""
    if amount is None:
        return "?"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.1f}k"
    return f"${amount:.0f}"


def _age_label(seen: str | None, now: dt.datetime) -> str:
    """How old the alerted price is — the market keeps moving after capture,
    so every alert says when its price was seen (lived: an alert quoted 4.80,
    the runner was 3.30 by the time the phone buzzed)."""
    if not seen:
        return ""
    with_tz = dt.datetime.fromisoformat(seen)
    if with_tz.tzinfo is None:
        with_tz = with_tz.replace(tzinfo=dt.UTC)
    minutes = max(0.0, (now - with_tz).total_seconds() / 60.0)
    label = "just now" if minutes < 1.0 else f"{minutes:.0f}m ago"
    return f" · price seen {label}"


def _kelly_stake(fair_prob: float, odds: float, bankroll: float) -> float:
    """Full-Kelly stake on a bankroll: f = (p*o - 1)/(o - 1). The caller only
    asks when edge > 0, so f is positive and < bankroll by construction."""
    if odds <= 1.0:
        return 0.0
    return max(0.0, bankroll * (fair_prob * odds - 1.0) / (odds - 1.0))


async def _watch_arb(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime | None = None
) -> int:
    """Cross-book arbitrage: scan fresh boards, alert each arb above the margin
    threshold. Dedupe buckets the margin so a GROWING arb re-fires."""
    from sportsdata_agents.quant.arbitrage import scan_arbs

    threshold = float(sub.params.get("threshold_pct", 1.0))
    hours = float(sub.params.get("hours", 1.0))
    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    bankroll = float(sub.params.get("bankroll", 100.0))
    arbs = await scan_arbs(
        session, hours=hours, threshold_pct=threshold,
        min_matched=float(sub.params.get("min_matched", 1000.0)),
        max_age_minutes=float(sub.params.get("max_age_minutes", 20.0)),
        limit=cap * 3, now=now)
    fired = 0
    for arb in arbs:
        if fired >= cap:
            break
        line = f" {arb['line']}" if arb["line"] else ""
        # equalised dollar stakes on the bankroll; same payout whichever wins
        profit = bankroll * (1.0 / arb["sum_inverse"] - 1.0)
        legs_text = "\n".join(
            f"• {leg['outcome']}: {leg['book']} {leg['odds']:.2f} — "
            f"stake ${leg['stake_share'] * bankroll:.2f}"
            + (f" · {_fmt_money(leg['matched'])} matched" if "matched" in leg else "")
            + _age_label(leg.get("seen"), now or dt.datetime.now(dt.UTC))
            for leg in arb["legs"]
        )
        message = (
            f":money_with_wings: ARB {arb['margin_pct']:.2f}% [{arb['sport']}] {arb['fixture']}\n"
            f"{arb['market']}{line} — on a ${bankroll:.0f} bankroll "
            f"(locked profit ${profit:.2f}):\n{legs_text}\n"
            f"_gross margin — verify every leg is live; exchange legs pay fees; "
            f"books may limit or void_"
        )
        books = ",".join(sorted({leg["book"] for leg in arb["legs"]}))
        bucket = int(arb["margin_pct"] / 0.5)  # re-fire when the margin grows a band
        key = f"arb:{arb['fixture_id']}:{arb['market']}:{arb['line']}:{books}:{bucket}"
        if await _fire(session, sub, kind="arb", key=key, message=message,
                       payload=arb, pusher=pusher):
            fired += 1
    return fired


async def _watch_exchange_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime | None = None
) -> int:
    """Book price vs the de-vigged exchange fair on the same fixture — the
    model-free value signal (the exchange's money is the opinion). Dedupe
    buckets the edge so a GROWING premium re-fires."""
    from sportsdata_agents.quant.arbitrage import scan_exchange_premium

    exchange_book = str(sub.params.get("exchange_book", "Betfair"))
    min_edge = float(sub.params.get("min_edge_pct", 3.0))
    hours = float(sub.params.get("hours", 1.0))
    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    candidates = await scan_exchange_premium(
        session, exchange_book=exchange_book, hours=hours,
        min_edge_pct=min_edge,
        min_matched=float(sub.params.get("min_matched", 1000.0)),
        limit=cap * 3, now=now)
    fired = 0
    for candidate in candidates:
        if fired >= cap:
            break
        bankroll = float(sub.params.get("bankroll", 100.0))
        kelly = _kelly_stake(1.0 / candidate["exchange_fair_odds"],
                             candidate["odds"], bankroll)
        message = (
            f":scales: exchange premium +{candidate['edge_pct']:.1f}% "
            f"[{candidate['sport']}] {candidate['fixture']}\n"
            f"{candidate['book']} pays {candidate['odds']:.2f} on "
            f"{candidate['market']} · {candidate['outcome']} — "
            f"{exchange_book} fair {candidate['exchange_fair_odds']:.2f} "
            f"({_fmt_money(candidate.get('exchange_matched'))} matched)\n"
            f"kelly ${kelly:.2f} on ${bankroll:.0f}"
            f"{_age_label(candidate.get('seen'), now or dt.datetime.now(dt.UTC))}\n"
            f"_vs de-vigged exchange back prices; verify the leg is live_"
        )
        bucket = int(candidate["edge_pct"] / 2.0)  # re-fire when the edge grows a band
        key = (f"exchange_value:{candidate['fixture_id']}:{candidate['market']}"
               f":{candidate['outcome']}:{candidate['book']}:{bucket}")
        payload = {**candidate, "kelly_stake": round(kelly, 2), "bankroll": bankroll}
        if await _fire(session, sub, kind="exchange_value", key=key, message=message,
                       payload=payload, pusher=pusher):
            fired += 1
    return fired


async def _event_display_name(session: AsyncSession, event_id: str) -> str | None:
    """The human event name for an alert — snapshots carry it, ids do not."""
    from sportsdata_agents.data.models import OddsSnapshot

    return (await session.execute(
        select(OddsSnapshot.event_name)
        .where(OddsSnapshot.event_external_id == event_id, OddsSnapshot.event_name != "")
        .order_by(OddsSnapshot.captured_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _watch_racing_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime | None = None
) -> int:
    """One book out from Betfair (or the pack) on a race, per runner — alerts
    carry the venue/race label, the HORSE'S NAME and saddle number, never ids."""
    from sportsdata_agents.quant.racing_value import scan_racing_value

    min_edge = float(sub.params.get("min_edge_pct", 8.0))
    hours = float(sub.params.get("hours", 0.75))
    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    candidates = await scan_racing_value(
        session, exchange_book=str(sub.params.get("exchange_book", "Betfair")),
        hours=hours, min_edge_pct=min_edge,
        max_fair_odds=float(sub.params.get("max_fair_odds", 12.0)),
        max_staleness_minutes=float(sub.params.get("max_staleness_minutes", 10.0)),
        min_matched=float(sub.params.get("min_matched", 500.0)),
        exclude_books=tuple(sub.params.get("exclude_books", ["FanDuel"])),
        limit=cap * 3, now=now)
    fired = 0
    for candidate in candidates:
        if fired >= cap:
            break
        number = f" (#{candidate['runner_number']})" if candidate.get("runner_number") else ""
        jump = ""
        if candidate.get("start_time"):
            jump = f" · jumps {candidate['start_time'][11:16]}"
        traded = ""
        if candidate.get("exchange_matched") is not None:
            traded = f" ({_fmt_money(candidate['exchange_matched'])} matched)"
        bankroll = float(sub.params.get("bankroll", 100.0))
        kelly = _kelly_stake(1.0 / candidate["fair_odds"], candidate["odds"], bankroll)
        message = (
            f":racehorse: racing value +{candidate['edge_pct']:.1f}% — "
            f"{candidate['race']}\n"
            f"{candidate['book']} pays {candidate['odds']:.2f} on "
            f"{candidate['runner']}{number} vs {candidate['versus']} "
            f"fair {candidate['fair_odds']:.2f}{traded}{jump}\n"
            f"kelly ${kelly:.2f} on ${bankroll:.0f}"
            f"{_age_label(candidate.get('seen'), now or dt.datetime.now(dt.UTC))}"
            f" — check the live price before betting"
        )
        bucket = int(candidate["edge_pct"] / 3.0)
        key = (f"racing_value:{candidate['race']}:{candidate['runner']}"
               f":{candidate['book']}:{bucket}")
        payload = {**candidate, "kelly_stake": round(kelly, 2), "bankroll": bankroll}
        if await _fire(session, sub, kind="racing_value", key=key, message=message,
                       payload=payload, pusher=pusher):
            fired += 1
    return fired


async def _watch_prediction_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime | None = None
) -> int:
    """Kalshi vs Polymarket disagreeing on the SAME question — value on markets
    no bookmaker offers (elections, geopolitics, crypto, culture). Alerts carry
    the plain-English question and outcome, never tickers."""
    from sportsdata_agents.quant.prediction_bridge import scan_prediction_disagreements

    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    candidates = await scan_prediction_disagreements(
        session,
        hours=float(sub.params.get("hours", 6.0)),
        min_edge_pct=float(sub.params.get("min_edge_pct", 10.0)),
        q_threshold=float(sub.params.get("q_threshold", 0.7)),
        prob_band=(float(sub.params.get("min_prob", 0.05)),
                   float(sub.params.get("max_prob", 0.95))),
        min_volume=float(sub.params.get("min_volume", 100.0)),
        max_staleness_minutes=float(sub.params.get("max_staleness_minutes", 90.0)),
        limit=cap * 3, now=now)
    fired = 0
    for candidate in candidates:
        if fired >= cap:
            break
        other = "Polymarket" if candidate["back"] == "Kalshi" else "Kalshi"
        bankroll = float(sub.params.get("bankroll", 100.0))
        kelly = _kelly_stake(1.0 / candidate["fair_odds"], candidate["back_odds"], bankroll)
        message = (
            f":crystal_ball: prediction value +{candidate['edge_pct']:.1f}% — "
            f"{candidate['question']}\n"
            f"{candidate['back']} pays {candidate['back_odds']:.2f} on "
            f"{candidate['outcome']} vs {other} fair {candidate['fair_odds']:.2f}"
            f" (K vol {_fmt_money(candidate.get('kalshi_volume'))} · "
            f"P vol {_fmt_money(candidate.get('polymarket_volume'))})\n"
            f"kelly ${kelly:.2f} on ${bankroll:.0f}"
            f" — confirm both platforms settle the question the same way"
        )
        bucket = int(candidate["edge_pct"] / 5.0)
        key = (f"prediction_value:{candidate['polymarket_event']}"
               f":{candidate['outcome']}:{candidate['back']}:{bucket}")
        payload = {**candidate, "kelly_stake": round(kelly, 2), "bankroll": bankroll}
        if await _fire(session, sub, kind="prediction_value", key=key, message=message,
                       payload=payload, pusher=pusher):
            fired += 1
    return fired


async def _watch_stat_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime
) -> int:
    """Player-prop ladder inconsistency: fit the entity-stat's model from the
    book's OWN threshold ladder (via the engine seam), then flag rungs that
    disagree with the ladder's fitted level — the consistency edge on props.

    Reads the structured stat lines the Dabble feed captures (meta carries
    player/stat/stat_line/line_type on each priced selection); O/U pairs at
    the same line de-vig into anchors that pin the fit's level. Degrades
    cleanly with no engine configured."""
    import math

    from sportsdata_agents.quant.engines import EngineUnavailable, resolve_engine

    try:
        engine = resolve_engine()
    except (EngineUnavailable, ValueError) as e:
        logger.info("stat_value watch %s: engine unavailable (%s)", sub.name, e)
        return 0
    if engine is None:
        logger.info("stat_value watch %s: no engine configured — skipping", sub.name)
        return 0

    book = str(sub.params.get("book", "Dabble"))
    min_edge = float(sub.params.get("min_edge_pct", 5.0))
    hours = float(sub.params.get("hours", 2.0))
    min_rungs = int(sub.params.get("min_rungs", 3))
    max_rmse = float(sub.params.get("max_rmse_log", 0.08))
    cap = int(sub.params.get("max_alerts_per_cycle", 5))

    from sportsdata_agents.data.models import OddsSnapshot

    rows = (await session.execute(
        select(OddsSnapshot).where(
            OddsSnapshot.book == book,
            OddsSnapshot.captured_at > now - dt.timedelta(hours=hours),
        ).order_by(OddsSnapshot.captured_at)
    )).scalars().all()
    # latest quote per (event, player, stat, line, side); meta marks prop rows
    ladders: dict[tuple[str, str, str], dict[tuple[float, str], float]] = {}
    names: dict[str, str] = {}
    sports: dict[str, str] = {}
    for row in rows:
        meta = row.meta or {}
        player, stat, line = meta.get("player"), meta.get("stat"), meta.get("stat_line")
        side = str(meta.get("line_type", "")).lower()
        if not player or not stat or line is None or side not in ("over", "under"):
            continue
        key = (row.event_external_id, str(player), str(stat))
        ladders.setdefault(key, {})[(float(line), side)] = float(row.odds)
        names[row.event_external_id] = row.event_name
        sports[row.event_external_id] = row.sport

    fired = 0
    for (event_id, player, stat), quotes_by_rung in sorted(ladders.items()):
        if fired >= cap:
            break
        lines = sorted({line for line, _ in quotes_by_rung})
        thresholds = {math.ceil(line) for line in lines}
        if len(thresholds) < min_rungs:
            continue  # the ladder's shape is unidentified — nothing to compare
        seam_quotes: list[dict[str, Any]] = []
        for line in lines:
            over = quotes_by_rung.get((line, "over"))
            under = quotes_by_rung.get((line, "under"))
            threshold = math.ceil(line)
            if over and under:
                p_over = (1.0 / over) / (1.0 / over + 1.0 / under)
                seam_quotes.append({"threshold": threshold, "odds": 1.0 / p_over,
                                    "devigged": True})
            elif over:
                seam_quotes.append({"threshold": threshold, "odds": over})
        if len({q["threshold"] for q in seam_quotes}) < 2:
            continue
        try:
            fit = engine.stat_prices(player, stat, seam_quotes, sorted(thresholds))
        except (EngineUnavailable, ValueError) as e:
            logger.info("stat_value: could not fit %s/%s %s: %s", event_id, player, stat, e)
            continue
        if float(fit.get("fit", {}).get("rmse_log", 9.9)) > max_rmse:
            continue  # the ladder does not agree with ITSELF enough to trust a fit
        fair = {int(p["line"]): float(p["fair_probability"]) for p in fit.get("prices", [])}
        for (line, side), odds in sorted(quotes_by_rung.items()):
            if fired >= cap:
                break
            survival = fair.get(math.ceil(line))
            if survival is None:
                continue
            prob = survival if side == "over" else 1.0 - survival
            edge_pct = (odds * prob - 1.0) * 100.0
            if edge_pct < min_edge:
                continue
            fitted_fair = 1.0 / prob if prob > 0 else float("inf")
            message = (
                f":dart: stat value +{edge_pct:.1f}% [{sports.get(event_id, '?')}] "
                f"{names.get(event_id, event_id)}\n"
                f"{book} · {player} {stat} {side} {line} — "
                f"book {odds:.2f} vs ladder-fitted fair {fitted_fair:.2f} "
                f"(mu {fit['model']['mu']:.1f}, {len(seam_quotes)} rungs)"
            )
            bucket = int(edge_pct / 2.0)
            alert_key = f"stat_value:{event_id}:{player}:{stat}:{line}:{side}:{bucket}"
            if await _fire(session, sub, kind="stat_value", key=alert_key, message=message,
                           payload={"event_external_id": event_id, "player": player,
                                    "stat": stat, "line": line, "side": side,
                                    "odds": odds, "edge_pct": round(edge_pct, 2),
                                    "fitted_fair": round(fitted_fair, 3),
                                    "mu": fit["model"]["mu"], "book": book},
                           pusher=pusher):
                fired += 1
    return fired


async def _push_digest(
    session: AsyncSession, sub: Subscription, pusher: Pusher, now: dt.datetime
) -> bool:
    """For digest watches (params.digest_hours): one summary push of everything
    that fired since the last digest, instead of a message per alert."""
    every = float(sub.params.get("digest_hours", 0) or 0)
    if every <= 0:
        return False
    last = sub.params.get("last_digest_at")
    last_at = dt.datetime.fromisoformat(last) if last else None
    if last_at is not None and now - last_at < dt.timedelta(hours=every):
        return False
    pending = (
        await session.execute(
            select(Alert)
            .where(Alert.subscription_id == sub.id, Alert.pushed.is_(False))
            .order_by(Alert.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    if pending:
        lines = [a.message.splitlines()[0] for a in pending[:15]]
        more = f"\n…and {len(pending) - 15} more" if len(pending) > 15 else ""
        summary = (f":newspaper: digest — {sub.name}: {len(pending)} alerts in the last "
                   f"{every:g}h\n" + "\n".join(lines) + more)
        try:
            ok = await pusher(sub, summary)
        except Exception as e:
            logger.warning("digest push failed for %s: %s", sub.name, e)
            ok = False
        if ok:
            for alert in pending:
                alert.pushed = True
    sub.params = {**sub.params, "last_digest_at": now.isoformat()}
    return bool(pending)


OUTCOME_MIN_AGE_S = 5 * 60  # measure after the actionable window has passed
OUTCOME_MAX_AGE_S = 60 * 60  # too old to re-measure meaningfully


async def measure_arb_outcomes(session: AsyncSession, *, now: dt.datetime) -> int:
    """Honesty loop: 5+ minutes after an arb or value alert fires, re-measure
    the SAME opportunity and stamp the outcome into the payload — "the watch
    fired" only matters if it was still takeable when a human could have acted.
    The alert_quality ops tool aggregates these for the weekly eval."""
    from sportsdata_agents.quant.arbitrage import arb_margin_now

    pending = (
        await session.execute(
            select(Alert).where(
                Alert.kind.in_(("arb", "value")),
                Alert.created_at <= now - dt.timedelta(seconds=OUTCOME_MIN_AGE_S),
                Alert.created_at >= now - dt.timedelta(seconds=OUTCOME_MAX_AGE_S),
            )
        )
    ).scalars().all()
    measured = 0
    for alert in pending:
        payload = dict(alert.payload or {})
        if "outcome" in payload:
            continue
        if alert.kind == "arb":
            if not payload.get("fixture_id"):
                continue
            margin_after = await arb_margin_now(
                session,
                fixture_id=str(payload["fixture_id"]),
                market=str(payload.get("market", "h2h")),
                line=str(payload.get("line", "")),
                now=now,
            )
            payload["outcome"] = {
                "margin_pct_after": margin_after,
                "still_arb": bool(margin_after is not None and margin_after > 0),
                "measured_at": now.isoformat(),
            }
        else:  # value: the edge at the CURRENT listed price
            prob = payload.get("prob")
            if prob is None or not payload.get("event_external_id"):
                continue  # pre-enrichment alerts can't be re-measured
            row = (
                await session.execute(
                    select(OddsSnapshot.odds)
                    .where(
                        OddsSnapshot.provider == str(payload.get("provider", "")),
                        OddsSnapshot.event_external_id == str(payload["event_external_id"]),
                        OddsSnapshot.market == str(payload.get("market", "")),
                        OddsSnapshot.selection == str(payload.get("selection", "")),
                        OddsSnapshot.captured_at >= now - dt.timedelta(hours=1),
                    )
                    .order_by(OddsSnapshot.captured_at.desc())
                    .limit(1)
                )
            ).scalar()
            edge_after = (float(prob) * float(row) - 1.0) * 100.0 if row is not None else None
            payload["outcome"] = {
                "edge_pct_after": round(edge_after, 2) if edge_after is not None else None,
                "still_value": bool(
                    edge_after is not None
                    and edge_after >= float(payload.get("min_edge_pct", 0))
                ),
                "measured_at": now.isoformat(),
            }
        alert.payload = payload  # reassign: JSON columns persist on attribute set
        measured += 1
    return measured


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
                elif sub.kind == "racing_value":
                    fired = await _watch_racing_value(session, sub, push, now=now)
                elif sub.kind == "prediction_value":
                    fired = await _watch_prediction_value(session, sub, push, now=now)
                elif sub.kind == "stat_value":
                    fired = await _watch_stat_value(session, sub, push, now=now)
                elif sub.kind == "exchange_value":
                    fired = await _watch_exchange_value(session, sub, push, now=now)
                elif sub.kind == "arb":
                    fired = await _watch_arb(session, sub, push, now=now)
                elif sub.kind == "model_value":
                    fired = await _watch_model_value(session, sub, push, now=now)
                else:
                    logger.warning("unknown watch kind %s (subscription %s)", sub.kind, sub.id)
                    continue
                sub.cursor = now
                report["alerts"] += fired
                if await _push_digest(session, sub, push, now):
                    report["digests"] = report.get("digests", 0) + 1
            except Exception as e:  # one broken watch must not sink the pass
                logger.warning("watch %s (%s) failed: %s", sub.name, sub.kind, e)
                report[f"error:{sub.name}"] = str(e)
        try:
            report["outcomes_measured"] = await measure_arb_outcomes(session, now=now)
        except Exception as e:  # measurement is bookkeeping — never sink the pass
            logger.warning("arb outcome measurement failed: %s", e)
        await session.commit()
    return report
