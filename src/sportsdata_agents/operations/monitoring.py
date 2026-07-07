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


async def _cross_book_quotes(session: AsyncSession, row: Price) -> dict[str, float]:
    """The same market at every OTHER book mapped to this event's fixture —
    {book: best odds}. Side-relative selections (home/away) translate between
    books' listing orders with the settlement-grade name matching; when the
    event isn't resolved onto a fixture yet, the map is simply empty."""
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
        return {}
    siblings = (
        await session.execute(
            select(Event).where(
                Event.fixture_id == mapping.fixture_id,
                Event.id != mapping.id,
            )
        )
    ).scalars().all()
    if not siblings:
        return {}
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
    return quotes


async def _context(session: AsyncSession, row: Price) -> dict[str, Any]:
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
    matched = (snap.meta or {}).get("total_matched") if snap else None
    money_kind = (snap.meta or {}).get("money_kind") if snap else None
    race = str((snap.meta or {}).get("race") or "") if snap else ""
    if race and race.split()[0] not in event:
        event = f"{event} · {race}"  # Betfair names the MEETING; the race rides meta
    return {"event": event, "sport": sport, "selection": selection,
            "who": str(who).strip() if who else None,
            "start_time": snap.start_time if snap else None, "matched": matched,
            "money_kind": money_kind}


_LABEL_ACRONYMS = {"afl", "nrl", "nba", "nfl", "mlb", "nhl", "mma", "ufc", "wnba", "npb"}


def _sport_label(sport: Any) -> str:
    """Display casing for a warehouse sport label — acronym sports stay
    uppercase ("AFL Football", not "Afl Football")."""
    words = str(sport or "?").replace("_", " ").split()
    return " ".join(w.upper() if w in _LABEL_ACRONYMS else w.capitalize() for w in words)


def _match(row: Price, params: dict[str, Any]) -> bool:
    if row.sport in (params.get("exclude_sports") or ()):
        return False
    if params.get("engine_sports_only") and row.sport not in _engine_labels():
        return False
    if row.market in (params.get("exclude_markets") or ()):
        return False
    markets = params.get("markets")
    if markets:
        # allowlist mode keeps single-book novelty markets out; prefix match
        # because books suffix their variants ("h2h - match (regular time)")
        allowed = [str(m).lower() for m in
                   ([markets] if isinstance(markets, str) else list(markets))]
        base = row.market.lower()
        if not any(base == m or base.startswith(f"{m} ") for m in allowed):
            return False
    for field in ("sport", "market", "selection", "book", "provider"):
        want = params.get(field)
        if want and getattr(row, field) != want:
            return False
    return True


async def _group_key(session: AsyncSession, row: Price, ctx: dict[str, Any]) -> tuple[str, str]:
    """The (event, selection) identity SHARED across books — races join on the
    venue+R<n> board key, sports on the resolved fixture; the selection identity
    is the runner/team name when the snapshot carries one (number-keyed books
    name the runner in meta), else the raw selection. This is what lets one
    market move fire ONE alert instead of one per book."""
    if ctx["sport"] in _RACING_LABELS:
        event_key = _racing_board_key(str(ctx["event"])).lower()
    else:
        fixture = (await session.execute(
            select(Event.fixture_id).where(Event.provider == row.provider,
                                           Event.external_id == row.event_external_id)
        )).scalar_one_or_none()
        event_key = str(fixture) if fixture else f"{row.provider}:{row.event_external_id}"
    who = ctx.get("who")
    sel_key = str(who or row.selection).strip().lower()
    return event_key, sel_key


def _started(start_time: Any) -> bool:
    """Has the event already jumped/kicked off? In-play prices move for game
    reasons (a goal, a set), which the pre-match watches must not read as
    market signal."""
    if start_time is None:
        return False
    when = start_time if start_time.tzinfo else start_time.replace(tzinfo=dt.UTC)
    return when <= dt.datetime.now(dt.UTC)


_RACING_LABELS = ("horse_racing", "greyhound_racing", "harness_racing",
                  "thoroughbred_racing")


def _racing_board_key(ctx_event: str) -> str:
    """The books' race identity from an alert context event label. Book rows
    are already "Venue R6"; exchange rows read "Gosford (AUS) 7th Jul · R10
    388m Gr5" (meeting + meta race) — reduce both to "Venue R<n>"."""
    import re as _re

    base, _, race = ctx_event.partition(" · ")
    match = _re.search(r"\bR(\d+)\b", race or base)
    venue = base.split(" (")[0].strip()
    if race and match:
        return f"{venue} R{match.group(1)}"
    return base.strip()


async def _racing_board(session: AsyncSession, event_name: str, market: str,
                        selection: str, book: str) -> dict[str, float]:
    """Other books' latest price for the same runner — racing events don't map
    through the fixture resolver; the join is the venue token + R<n> tag
    ("MANAWATU R1" / "Manawatu R1" / "Mountaineer Park R3" all match).

    Runners are keyed TWO ways across the industry — saddle number (FanDuel,
    Ladbrokes, PointsBet, BetR, Sportsbet, TAB) versus runner name (Betfair,
    Unibet) — so the board translates through the number↔name bridge built
    from the number-keyed rows' own runner meta. Selection string equality
    alone left half the industry NA on fully priced races."""
    if not event_name:
        return {}
    import re as _re

    from sqlalchemy import func

    race_match = _re.search(r"\bR(\d+)\b", event_name, _re.IGNORECASE)
    venue_token = event_name.split()[0].lower() if event_name.split() else ""
    if not race_match or len(venue_token) < 3:
        return {}
    race_tag = f"r{race_match.group(1)}"
    rows = (await session.execute(
        select(OddsSnapshot.book, OddsSnapshot.odds, OddsSnapshot.selection,
               OddsSnapshot.event_name, OddsSnapshot.meta)
        .where(func.lower(OddsSnapshot.event_name).like(f"{venue_token}%"),
               OddsSnapshot.market == market,
               OddsSnapshot.captured_at > dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30))
        .order_by(OddsSnapshot.captured_at.desc()).limit(800)
    )).all()
    # race-scoped rows, newest first per (book, selection)
    race_rows: list[tuple[str, float, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for other_book, odds, sel, name, meta in rows:
        tags = {w.lower() for w in _re.findall(r"\bR\d+\b", name or "", _re.IGNORECASE)}
        if race_tag not in tags or (other_book, str(sel)) in seen:
            continue
        seen.add((other_book, str(sel)))
        race_rows.append((other_book, float(odds), str(sel), meta or {}))
    number_to_name: dict[str, str] = {}
    for _b, _o, sel, meta in race_rows:
        runner = str(meta.get("runner") or "").strip().lower()
        if sel.isdigit() and runner:
            number_to_name.setdefault(sel, runner)
    name_to_number = {name: num for num, name in number_to_name.items()}
    target = str(selection).strip().lower()
    if target.isdigit():
        number, name = target, number_to_name.get(target, "")
    else:
        number, name = name_to_number.get(target, ""), target
    quotes: dict[str, float] = {}
    for other_book, odds, sel, meta in race_rows:
        if other_book == book:
            continue
        matched = meta.get("total_matched")
        try:
            if matched is not None and float(matched) < 100.0:
                continue  # a near-untraded exchange row is a stray order, not a price
        except (TypeError, ValueError):
            pass
        sel_l = sel.lower()
        runner = str(meta.get("runner") or "").strip().lower()
        if ((number and sel_l == number) or (name and sel_l == name)
                or (name and runner == name)):
            quotes.setdefault(other_book, odds)
    return quotes


_PACK_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}


async def _coverage_pack(session: AsyncSession, sport: str) -> tuple[str, ...]:
    """Books that actually fed this sport in the last 24 hours — the board's
    always-show pack. NA next to one of these means "covers the sport, hasn't
    priced this market": real information. The old hardcoded racing pack put
    books that never send tennis on tennis boards (walls of meaningless NA)
    and left Dabble — which feeds everything — off every board."""
    import time

    cached = _PACK_CACHE.get(sport)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1]
    rows = (await session.execute(
        select(Price.book).distinct().where(
            Price.sport == sport,
            Price.changed_at > dt.datetime.now(dt.UTC) - dt.timedelta(hours=24))
    )).scalars().all()
    pack = tuple(sorted(rows))
    _PACK_CACHE[sport] = (time.monotonic(), pack)
    return pack


def _format_board(quotes: dict[str, float], sharps: list[str],
                  include: tuple[str, ...] = ()) -> str:
    """The industry board, SHARPS FIRST then best price. Books in ``include``
    always appear — NA when they have not priced it — so a missing quote is
    visible information, never silence."""
    ordered: list[tuple[str, float | None]] = []
    seen: set[str] = set()
    for book in [*sharps, *include]:
        if book not in seen:
            ordered.append((book, quotes.get(book)))
            seen.add(book)
    for book, odds in sorted(quotes.items(), key=lambda kv: -kv[1]):
        if book not in seen:
            ordered.append((book, odds))
            seen.add(book)
    priced = [(b, o) for b, o in ordered if o is not None]
    if not priced:
        return ""
    if len(priced) < 3:
        # a wall of NA is useless — say the true thing in one line
        few = " · ".join(f"{b} {o:.2f}" for b, o in priced)
        note = ("no other book has priced this market yet" if len(priced) == 1
                else "the only books with a price so far")
        return f"\nacross books: {few} — {note}"
    board = " · ".join(f"{b} {o:.2f}" if o is not None else f"{b} NA"
                       for b, o in ordered[:10])
    return f"\nacross books: {board}"


def _thin_exchange(sub: Subscription, ctx: dict[str, Any]) -> bool:
    """True = suppress: the row is an EXCHANGE quote (it carries matched-money
    meta) with less traded than exchange_min_matched — a near-untraded market's
    "moves" are stray orders appearing and vanishing, not the market walking
    (lived: a leftover 1.81 back on a 100-1 horse read as a 2,552% drift)."""
    matched = ctx.get("matched")
    if matched is None:
        return False  # not an exchange row — books have no matched concept
    try:
        return float(matched) < float(sub.params.get("exchange_min_matched", 1000.0))
    except (TypeError, ValueError):
        return False


def _exchange_alone(sub: Subscription, ctx: dict[str, Any],
                    quotes: dict[str, float]) -> bool:
    """True = suppress: an EXCHANGE price moving on a race where every book is
    still SP-only (no fixed odds captured anywhere). There is nothing to take —
    you cannot bet a price that hasn't been posted; the actionable moment is
    when the books OPEN, and the racing value scan catches that within a
    cycle. On by default for racing rows; exchange_needs_book_prices=false
    restores the old behaviour."""
    if not bool(sub.params.get("exchange_needs_book_prices", True)):
        return False
    return (ctx.get("sport") in _RACING_LABELS
            and ctx.get("matched") is not None  # exchange rows carry matched
            and not quotes)


def _drift_suppressed(sub: Subscription, drifting: bool, odds: float,
                      engine_fair: float | None, quotes: dict[str, float]) -> bool:
    """True = suppress a DRIFT alert: nobody prices it shorter than the
    drifted-to price (no other book, not the engine) — a friendless drift is
    an event signal (scratching, team news), not value."""
    if not drifting or not sub.params.get("drift_value_only"):
        return False
    if engine_fair is not None and engine_fair < float(odds):
        return False
    return not any(o < float(odds) for o in quotes.values())


def _engine_labels() -> set[str]:
    """Every warehouse sport label the engine prices (the slate's map)."""
    from sportsdata_agents.quant.slate import SLATE_SPORTS

    return {label for _sport, label in SLATE_SPORTS}


def _lacks_clear_ev(sub: Subscription, odds: float, engine_fair: float | None) -> bool:
    """True = suppress: the watch demands a POSITIVE engine edge
    (min_engine_edge_pct) and this price doesn't show one — including when
    the slate simply hasn't priced the market (no fair = no demonstrated EV)."""
    floor = sub.params.get("min_engine_edge_pct")
    if floor is None:
        return False
    if engine_fair is None or engine_fair <= 0:
        return True
    return (float(odds) / engine_fair - 1.0) * 100.0 < float(floor)


def _engine_veto(sub: Subscription, odds: float, engine_fair: float | None,
                 quotes: dict[str, float]) -> bool:
    """True = suppress: the ENGINE says the price is below fair (engine fair
    odds >= the offer) and no sharp book corroborates value by quoting UNDER
    the offer. Off by default; ``engine_gate=true`` turns it on per watch."""
    if not sub.params.get("engine_gate") or engine_fair is None:
        return False
    if engine_fair < float(odds):
        return False  # the engine itself sees value — let it through
    sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
    return not any(quotes.get(b, float("inf")) < float(odds) for b in sharps)


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
    if float(subscription.params.get("digest_hours", 0) or 0) > 0 or _in_quiet_hours(subscription):
        pushed = False  # digest watches batch pushes; quiet hours keep the
        # phone silent overnight — either way the alert ROW is still the record
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
    """One alert per MARKET move, not per book: every book that moved on the
    same event/market/selection in the pass lands in a single message with the
    shared board (the old per-book keying sent five near-identical pings for
    one race)."""
    threshold = float(sub.params.get("threshold_pct", 5.0))
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.prev_odds is None or not _match(row, sub.params):
            continue
        move = _pct_move(float(row.prev_odds), float(row.odds))
        if move < threshold:
            continue
        direction = "shortened" if float(row.odds) < float(row.prev_odds) else "drifted"
        ctx = await _context(session, row)
        if bool(sub.params.get("pre_match_only", True)) and _started(ctx["start_time"]):
            continue  # in-play prices move for game reasons, not market ones
        if _thin_exchange(sub, ctx):
            continue
        engine_fair = await _engine_fair_for(
            session, row.market, row.selection, event_id=row.event_external_id)
        quotes = await _cross_book_quotes(session, row)
        if not quotes and ctx["sport"] in _RACING_LABELS:
            quotes = await _racing_board(session, _racing_board_key(str(ctx["event"])),
                                         row.market, row.selection, row.book)
        if _exchange_alone(sub, ctx, quotes):
            continue
        if _engine_veto(sub, float(row.odds), engine_fair, quotes):
            continue
        if _lacks_clear_ev(sub, float(row.odds), engine_fair):
            continue
        if _drift_suppressed(sub, direction == "drifted", float(row.odds),
                             engine_fair, quotes):
            continue
        event_key, sel_key = await _group_key(session, row, ctx)
        group = groups.setdefault((event_key, row.market, sel_key, direction),
                                  {"movers": {}, "best": None})
        prior = group["movers"].get(row.book)
        if prior is None or move > prior[2]:
            group["movers"][row.book] = (float(row.prev_odds), float(row.odds), move)
        if group["best"] is None or move > group["best"]["move"]:
            group["best"] = {"row": row, "ctx": ctx, "move": move,
                             "engine_fair": engine_fair, "quotes": quotes}
    fired = 0
    for (event_key, market, sel_key, direction), group in groups.items():
        if fired >= cap:
            break
        best = group["best"]
        row, ctx, quotes = best["row"], best["ctx"], best["quotes"]
        engine_fair, move = best["engine_fair"], best["move"]
        direction_word = "shortened" if direction == "shortened" else "drifted out"
        movers = " · ".join(
            f"{b} {prev:.2f} to {new:.2f}"
            for b, (prev, new, _m) in sorted(group["movers"].items(),
                                             key=lambda kv: -kv[1][2]))
        engine_note = f"\nEngine fair {engine_fair:.2f}" if engine_fair else ""
        money_word = "pool" if (ctx.get("money_kind") == "pool") else "matched"
        traded = (f" · {_fmt_money(float(ctx['matched']))} {money_word}"
                  if ctx.get("matched") else "")
        jump = ""
        if ctx.get("start_time") and not _started(ctx["start_time"]):
            jump = f"\nStarts {_local_hhmm(ctx['start_time'].isoformat(), _tz_for(sub))}"
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        include = await _coverage_pack(session, str(ctx["sport"]))
        for mover, (_prev, new, _m) in group["movers"].items():
            quotes.setdefault(mover, new)  # every mover is a price, not NA
        message = (
            f":chart_with_upwards_trend: Line Move — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
            f"Market: {market} · Selection: {ctx['selection']}\n"
            f"Price {direction_word} {move:.1f} percent: {movers}"
            f"{engine_note}{traded}{jump}"
            + _format_board(quotes, sharps, include)
        )
        key = f"line_move:{event_key}:{market}:{sel_key}:{direction}"
        payload = {"move_pct": round(move, 2), "odds": float(row.odds),
                   "prev_odds": float(row.prev_odds or 0),
                   "books": sorted(group["movers"])}
        if await _fire(session, sub, kind="line_move", key=key, message=message,
                       payload=payload, pusher=pusher):
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
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for (book, event, market, selection), series in by_key.items():
        series.sort(key=lambda r: r.changed_at)
        # NET direction with tolerance: strict same-direction meant one tiny
        # counter-tick reset the streak and the alert arrived late or never.
        # A walk is a walk if moves against the trend stay <= 1 per 4 with it.
        signs = [1 if float(r.odds) > float(r.prev_odds or 0) else -1 for r in series]
        net = 1 if float(series[-1].odds) >= float(series[0].prev_odds or 0) else -1
        with_trend = sum(1 for s in signs if s == net)
        against = len(signs) - with_trend
        if not (with_trend >= min_moves and against <= max(1, with_trend // 4)):
            continue
        ctx = await _context(session, series[-1])
        if bool(sub.params.get("pre_match_only", True)) and _started(ctx["start_time"]):
            continue  # in-play prices move for game reasons, not market ones
        if _thin_exchange(sub, ctx):
            continue
        arrow = "drifting" if net == 1 else "steaming in"
        engine_fair = await _engine_fair_for(session, market, selection, event_id=event)
        quotes = await _cross_book_quotes(session, series[-1])
        if not quotes and ctx["sport"] in _RACING_LABELS:
            quotes = await _racing_board(session, _racing_board_key(str(ctx["event"])),
                                         market, selection, book)
        current = float(series[-1].odds)
        if _exchange_alone(sub, ctx, quotes):
            continue
        if _engine_veto(sub, current, engine_fair, quotes):
            continue
        if _lacks_clear_ev(sub, current, engine_fair):
            continue
        if _drift_suppressed(sub, arrow == "drifting", current, engine_fair, quotes):
            continue
        # one alert per WALKING MARKET, not per book: every book whose board
        # is walking the same way on this selection joins one message
        event_key, sel_key = await _group_key(session, series[-1], ctx)
        group = groups.setdefault((event_key, market, sel_key, arrow),
                                  {"streaks": {}, "best": None})
        group["streaks"][book] = (float(series[0].prev_odds or 0), current, len(series))
        if group["best"] is None or len(series) > group["best"]["moves"]:
            group["best"] = {"row": series[-1], "ctx": ctx, "moves": len(series),
                             "engine_fair": engine_fair, "quotes": quotes}
    fired = 0
    for (event_key, market, sel_key, arrow), group in groups.items():
        if fired >= cap:
            break
        best = group["best"]
        ctx, quotes, engine_fair = best["ctx"], best["quotes"], best["engine_fair"]
        direction_word = "drifting out" if arrow == "drifting" else "steaming in"
        streaks = " · ".join(
            f"{b} {prev:.2f} to {new:.2f} over {n} moves"
            for b, (prev, new, n) in sorted(group["streaks"].items(),
                                            key=lambda kv: -kv[1][2]))
        engine_note = f"\nEngine fair {engine_fair:.2f}" if engine_fair else ""
        money_word = "pool" if (ctx.get("money_kind") == "pool") else "matched"
        traded = (f" · {_fmt_money(float(ctx['matched']))} {money_word}"
                  if ctx.get("matched") else "")
        jump = ""
        if ctx.get("start_time") and not _started(ctx["start_time"]):
            jump = f"\nStarts {_local_hhmm(ctx['start_time'].isoformat(), _tz_for(sub))}"
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        include = await _coverage_pack(session, str(ctx["sport"]))
        for mover, (_prev, new, _n) in group["streaks"].items():
            quotes.setdefault(mover, new)  # every walking book is a price, not NA
        message = (
            f":fire: Steam — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
            f"Market: {market} · Selection: {ctx['selection']}\n"
            f"Price {direction_word}: {streaks}"
            f"{engine_note}{traded}{jump}"
            + _format_board(quotes, sharps, include)
        )
        # band the dedupe by streak length: the alert fires when the streak
        # first reaches min_moves (so the count reads exactly the threshold),
        # then AGAIN each time it doubles — a runaway steam says "8 moves",
        # "12 moves" instead of going silent after the first ping
        band = best["moves"] // max(min_moves, 1)
        key = f"steam:{event_key}:{market}:{sel_key}:{arrow}:{band}"
        payload = {"moves": best["moves"], "books": sorted(group["streaks"])}
        if await _fire(session, sub, kind="steam", key=key, message=message,
                       payload=payload, pusher=pusher):
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
            quotes = await _cross_book_quotes(session, latest)
            quotes.setdefault(latest.book, float(latest.odds))
            include = await _coverage_pack(session, str(ctx["sport"]))
            message = (
                f":moneybag: Model Edge — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
                f"{latest.book} · Market: {pred.market} · Selection: {ctx['selection']}\n"
                f"Model probability {float(pred.prob):.0%} at odds {float(latest.odds):.2f} "
                f"· Edge +{edge:.1f} percent"
                + _format_board(quotes, ["Pinnacle", "Betfair"], include)
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
            display = (await _event_display_name(session, pred.event_external_id)
                       or pred.event_external_id)
            message = (
                f":hourglass: Edge Gone — {display}\n"
                f"Market: {pred.market} · Selection: {pred.selection} — the edge "
                f"is now +{edge:.1f} percent, under the {min_edge:g} percent floor"
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
                f":crystal_ball: Model Value — {_sport_label(sport)} — {display}\n"
                f"{row_book} · market: {candidate['market']} · "
                f"selection: {candidate['selection']}{at_line}\n"
                f"Book price {candidate['odds']:.2f} against the model fair "
                f"{candidate['model_fair_odds']} · Edge +{candidate['edge_pct']:.1f} percent"
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
                    f":no_entry: Scratching Suspect — {_sport_label(sport)} — {event_name}\n"
                    f"Runner {selection} — no price updates since "
                    f"{_local_hhmm(when.isoformat(), _tz_for(sub))} while the rest "
                    f"of the card kept moving\n"
                    f"Check the racecard before betting — the runner may be scratched"
                )
                key = f"scratching:{provider}:{event}:{selection}"
                if await _fire(session, sub, kind="scratching", key=key, message=message,
                               payload={"last_seen": when.isoformat()}, pusher=pusher):
                    fired += 1
    return fired


async def _engine_fair_for(
    session: AsyncSession, market: Any, selection: Any, *,
    event_id: Any = None, fixture_id: Any = None,
) -> float | None:
    """The ENGINE's own fair odds for a market/selection, from the slate's
    recorded predictions (anchored engine:{sport} first — it sorts before the
    ratings/form artifacts alphabetically — then the book-free fairs). Looked
    up by the book's event id, or through the fixture mapping when only the
    shared fixture is known (exchange candidates). None when the slate hasn't
    priced it yet — the alert simply shows the market fair alone."""
    if not market or selection is None or (not event_id and not fixture_id):
        return None
    from sportsdata_agents.data.models import ModelArtifact

    stmt = (
        select(Prediction.prob)
        .join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
        .where(
            ModelArtifact.name.like("engine%"),
            # form fairs are STRUCTURED-only now (run-by-run history parsed
            # from racecards) — the approximation era's junk aged out under
            # the 6h freshness bound below
            Prediction.market == str(market),
            Prediction.selection == str(selection),
            # a fair from this morning is not a fair for this race
            Prediction.predicted_at > dt.datetime.now(dt.UTC) - dt.timedelta(hours=6),
        )
        .order_by(ModelArtifact.name, Prediction.predicted_at.desc())
        .limit(1)
    )
    if event_id:
        stmt = stmt.where(Prediction.event_external_id == str(event_id))
    else:
        import uuid as _uuid

        try:
            fixture_uuid = _uuid.UUID(str(fixture_id))
        except (ValueError, AttributeError):
            return None
        stmt = stmt.join(Event, Event.external_id == Prediction.event_external_id
                         ).where(Event.fixture_id == fixture_uuid)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    prob = float(row)
    return 1.0 / prob if 0.0 < prob < 1.0 else None


def _fmt_money(amount: float | None) -> str:
    """Traded-volume display for alerts: $16, $12.3k, $1.2M."""
    if amount is None:
        return "?"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.1f}k"
    return f"${amount:.0f}"


def _local_hhmm(iso: str | None, tz_name: str) -> str:
    """A UTC timestamp rendered as HH:MM on the user's wall clock — alerts
    printed raw UTC and read hours wrong to a human (lived: 'jumps 10:48'
    for a race jumping 20:48 in Melbourne)."""
    if not iso:
        return ""
    from zoneinfo import ZoneInfo

    when = dt.datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    try:
        return when.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")
    except Exception:  # unknown zone name — UTC beats a crash
        return when.strftime("%H:%M UTC")


def _tz_for(sub: Subscription) -> str:
    """Per-watch tz param wins; else the operator's env; else Melbourne."""
    import os

    return str(sub.params.get("tz")
               or os.environ.get("SPORTSDATA_AGENTS_TZ", "Australia/Melbourne"))


def _in_quiet_hours(sub: Subscription, now: dt.datetime | None = None) -> bool:
    """True inside the watch's quiet_hours window ('23-08', local hours) — the
    phone stays silent but the alert row is still written. A malformed spec
    reads as no quiet hours: silence must be something the user clearly asked for."""
    spec = str(sub.params.get("quiet_hours") or "")
    if "-" not in spec:
        return False
    try:
        start, end = (int(part) for part in spec.split("-", 1))
    except ValueError:
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return False
    from zoneinfo import ZoneInfo

    try:
        hour = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(_tz_for(sub))).hour
    except Exception:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps midnight (the usual case)


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
            f":money_with_wings: Arbitrage — {_sport_label(arb['sport'])} — {arb['fixture']}\n"
            f"Market: {arb['market']}{line} · Gross margin {arb['margin_pct']:.2f} percent\n"
            f"On a {_fmt_money(bankroll)} bankroll the locked profit is ${profit:.2f}:\n"
            f"{legs_text}\n"
            f"_Gross margin — verify every leg is live; exchange legs pay commission; "
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
        engine_fair = await _engine_fair_for(
            session, candidate["market"], candidate["outcome"],
            fixture_id=candidate.get("fixture_id"))
        engine_note = f" · Engine fair {engine_fair:.2f}" if engine_fair else ""
        message = (
            f":scales: Exchange Value — {_sport_label(candidate['sport'])} — "
            f"{candidate['fixture']}\n"
            f"{candidate['book']} · Market: {candidate['market']} · "
            f"Selection: {candidate['outcome']}\n"
            f"Book price {candidate['odds']:.2f} against the {exchange_book} fair "
            f"{candidate['exchange_fair_odds']:.2f}{engine_note} · "
            f"Edge +{candidate['edge_pct']:.1f} percent · "
            f"Money matched {_fmt_money(candidate.get('exchange_matched'))}\n"
            f"Suggested stake ${kelly:.2f} of {_fmt_money(bankroll)} bankroll"
            f"{_age_label(candidate.get('seen'), now or dt.datetime.now(dt.UTC))}\n"
            f"_Fair is the de-vigged exchange back price; verify the leg is live_"
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


async def _runner_form_note(session: AsyncSession, race_no: Any, start_time: Any,
                            runner_number: Any) -> str:
    """One line of form for the alerted runner, from the form store: the
    compact figures plus the parsed recent runs ("2nd of 7, 13 days ago")."""
    if race_no is None or runner_number is None or not start_time:
        return ""
    from sportsdata_agents.data.models import RaceForm

    try:
        start = dt.datetime.fromisoformat(str(start_time))
    except ValueError:
        return ""
    race = (await session.execute(
        select(RaceForm).where(
            RaceForm.race_number == int(race_no),
            RaceForm.start_time > start - dt.timedelta(minutes=10),
            RaceForm.start_time < start + dt.timedelta(minutes=10),
        ).order_by(RaceForm.captured_at.desc()).limit(3)
    )).scalars().all()
    for row in race:
        for runner in row.runners or []:
            if str(runner.get("number")) != str(runner_number):
                continue
            parts = []
            if runner.get("last_starts"):
                parts.append(f"figures {runner['last_starts']}")
            runs = runner.get("runs") or []
            if runs:
                latest = runs[0]
                parts.append(f"latest run {latest['position']} of "
                             f"{latest['field_size']}, "
                             f"{latest['age_days']:.0f} days ago")
            if runner.get("days_since_run") is not None and not runs:
                parts.append(f"{runner['days_since_run']} days since last run")
            if parts:
                return "\nForm: " + " · ".join(parts)
    return ""


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
        max_edge_pct=float(sub.params.get("max_edge_pct", 60.0)),
        max_fair_odds=float(sub.params.get("max_fair_odds", 12.0)),
        max_staleness_minutes=float(sub.params.get("max_staleness_minutes", 10.0)),
        min_matched=float(sub.params.get("min_matched", 500.0)),
        exclude_books=tuple(sub.params.get("exclude_books", ["FanDuel"])),
        min_consensus_books=int(sub.params.get("min_consensus_books", 3)),
        limit=cap * 3, now=now)
    fired = 0
    for candidate in candidates:
        if fired >= cap:
            break
        number = f" (#{candidate['runner_number']})" if candidate.get("runner_number") else ""
        jump = ""
        if candidate.get("start_time"):
            jump = f" · jumps {_local_hhmm(candidate['start_time'], _tz_for(sub))}"
        traded = ""
        if candidate.get("exchange_matched") is not None:
            traded = f" ({_fmt_money(candidate['exchange_matched'])} matched)"
        engine_note = ""
        engine_fair = await _engine_fair_for(
            session, "win", candidate.get("runner_number"),
            event_id=candidate.get("event_external_id"))
        if (sub.params.get("engine_gate") and engine_fair is not None
                and engine_fair >= candidate["odds"]
                and candidate.get("versus") != str(sub.params.get("exchange_book", "Betfair"))):
            continue  # engine says no value and no exchange corroboration
        if engine_fair is not None:
            engine_note = f" · Engine fair {engine_fair:.2f}"
            candidate = {**candidate, "engine_fair_odds": round(engine_fair, 2)}
        bankroll = float(sub.params.get("bankroll", 100.0))
        kelly = _kelly_stake(1.0 / candidate["fair_odds"], candidate["odds"], bankroll)
        message = (
            f":racehorse: Racing Value — {candidate['race']} — "
            f"edge +{candidate['edge_pct']:.1f} percent\n"
            f"Runner{number} {candidate['runner']}\n"
            f"Bet: {candidate['book']} win at {candidate['odds']:.2f}\n"
            f"Market fair {candidate['fair_odds']:.2f} "
            f"(versus {candidate['versus']}){engine_note}{traded}\n"
            f"Suggested stake {_fmt_money(kelly)} of {_fmt_money(bankroll)} "
            f"bankroll{jump}"
            + await _runner_form_note(session, candidate.get("race_no"),
                                      candidate.get("start_time"),
                                      candidate.get("runner_number"))
            + f"{_age_label(candidate.get('seen'), now or dt.datetime.now(dt.UTC))}\n"
              f"_Check the live price before betting_"
        )
        # dedupe ONCE per race+runner+book for the window — the old edge/3
        # bucket re-fired the same runner every few minutes as its edge drifted
        # up (e.g. +24% then +50% as the price firmed), spamming repeats. A
        # materially bigger opportunity (a whole 25-point band) still re-alerts.
        bucket = int(candidate["edge_pct"] / 25.0)
        key = (f"racing_value:{candidate['race']}:{candidate['runner']}"
               f":{candidate['book']}:{bucket}")
        board = await _racing_board(
            session, str(candidate["race"]), "win",
            str(candidate.get("runner_number") or ""), str(candidate["book"]))
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        message += _format_board(board, sharps)
        payload = {**candidate, "kelly_stake": round(kelly, 2), "bankroll": bankroll}
        if await _fire(session, sub, kind="racing_value", key=key, message=message,
                       payload=payload, pusher=pusher):
            fired += 1
    return fired


async def _watch_bsp_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime
) -> int:
    """Back on the EXCHANGE when the structured-form fair says the current
    Betfair price (a BSP candidate) is over the odds — the book-free racing
    opinion, bettable where prices always exist."""
    from sportsdata_agents.data.models import ModelArtifact, RaceForm

    exchange_book = str(sub.params.get("exchange_book", "Betfair"))
    min_edge = float(sub.params.get("min_edge_pct", 10.0))
    lead = float(sub.params.get("lead_minutes", 45.0))
    min_matched = float(sub.params.get("min_matched", 2000.0))
    commission = float(sub.params.get("commission_pct", 5.0)) / 100.0
    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    bankroll = float(sub.params.get("bankroll", 100.0))

    races = (await session.execute(
        select(RaceForm).where(RaceForm.start_time > now,
                               RaceForm.start_time < now + dt.timedelta(minutes=lead))
    )).scalars().all()
    fired = 0
    for race in races:
        if fired >= cap:
            break
        preds = (await session.execute(
            select(Prediction.selection, Prediction.prob)
            .join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
            .where(ModelArtifact.name == "engine-form:racing",
                   Prediction.event_external_id == race.race_key,
                   Prediction.market == "win",
                   Prediction.predicted_at > now - dt.timedelta(hours=6))
            .order_by(Prediction.predicted_at.desc())
        )).all()
        if not preds:
            continue
        prob_by_number: dict[str, float] = {}
        for sel, prob in preds:  # newest first — older passes never overwrite
            prob_by_number.setdefault(str(sel), float(prob))
        names = {str(r.get("number")): str(r.get("name") or "") for r in race.runners or []}
        for number, prob in sorted(prob_by_number.items(), key=lambda kv: -kv[1]):
            if fired >= cap or not 0.0 < prob < 1.0:
                continue
            name = names.get(number, "")
            if not name:
                continue
            snap = (await session.execute(
                select(OddsSnapshot).where(
                    OddsSnapshot.book == exchange_book,
                    OddsSnapshot.market == "win",
                    OddsSnapshot.selection == name.lower(),
                    OddsSnapshot.captured_at > now - dt.timedelta(minutes=10),
                    OddsSnapshot.start_time > now - dt.timedelta(minutes=5),
                    OddsSnapshot.start_time < now + dt.timedelta(minutes=lead + 5),
                ).order_by(OddsSnapshot.captured_at.desc()).limit(1)
            )).scalars().first()
            if snap is None:
                continue
            matched = (snap.meta or {}).get("total_matched")
            if matched is None or float(matched) < min_matched:
                continue
            back = float(snap.odds)
            effective = 1.0 + (back - 1.0) * (1.0 - commission)
            edge_pct = (effective * prob - 1.0) * 100.0
            if edge_pct < min_edge:
                continue
            kelly = _kelly_stake(prob, effective, bankroll)
            # the exchange row carries the BOOKS' race naming — use it for the
            # title (TAB's 3-letter venue codes read as noise) and the board
            board_key = _racing_board_key(snap.event_name or "")
            title_race = (board_key if len(str(race.venue_mnemonic)) <= 4
                          else f"{race.venue_mnemonic} Race {race.race_number}")
            quotes = await _racing_board(session, board_key, "win", str(number),
                                         exchange_book)
            engine_fair = await _engine_fair_for(session, "win", str(number),
                                                 event_id=race.race_key)
            anchored = ""
            if engine_fair is not None and abs(engine_fair - 1.0 / prob) > 0.01:
                anchored = f" · Market-model fair {engine_fair:.2f}"
            runner_runs = next((r.get("runs") or [] for r in race.runners or []
                                if str(r.get("number")) == str(number)), [])
            form_line = ""
            if runner_runs:
                recent = " then ".join(f"{r['position']} of {r['field_size']}"
                                       for r in runner_runs[:3])
                form_line = (f"\nForm: {recent} · latest run "
                             f"{runner_runs[0]['age_days']:.0f} days ago")
            sharps = [str(b) for b in sub.params.get("sharp_books",
                                                     ["Pinnacle", "Betfair"])]
            jump = _local_hhmm(race.start_time.isoformat(), _tz_for(sub))                 if race.start_time else "?"
            message = (
                f":crystal_ball: Exchange value from form — {title_race}\n"
                f"Runner {number} — {name.title()}\n"
                f"Back on {exchange_book} at {back:.2f} "
                f"(worth {effective:.2f} after {commission:.0%} commission)\n"
                f"Form fair price {1.0 / prob:.2f}{anchored} · Edge +{edge_pct:.1f}% · "
                f"Money matched {_fmt_money(float(matched))}\n"
                f"Suggested stake {_fmt_money(kelly)} of "
                f"{_fmt_money(bankroll)} bankroll · Race starts {jump}"
                f"{form_line}"
                + _format_board(quotes, sharps, await _coverage_pack(session, str(snap.sport)))
                + "\n_Consider Betfair Starting Price if the current price slips_"
            )
            key = f"bsp_value:{race.race_key}:{number}:{int(edge_pct / 5)}"
            payload = {"race_key": race.race_key, "runner": name, "number": number,
                       "back": back, "form_fair": round(1.0 / prob, 2),
                       "edge_pct": round(edge_pct, 2), "matched": matched,
                       "kelly_stake": round(kelly, 2),
                       "provider": "sportsbet_racing", "event_external_id": race.race_key}
            if await _fire(session, sub, kind="bsp_value", key=key, message=message,
                           payload=payload, pusher=pusher):
                fired += 1
    return fired


async def _watch_back_lay(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime | None = None
) -> int:
    """Back at a book, lay the same outcome on the exchange — a risk-free
    margin net of commission (one outcome, two sides; not the cross-book arb)."""
    from sportsdata_agents.quant.arbitrage import scan_back_lay

    cap = int(sub.params.get("max_alerts_per_cycle", 5))
    bankroll = float(sub.params.get("bankroll", 100.0))
    candidates = await scan_back_lay(
        session,
        exchange_book=str(sub.params.get("exchange_book", "Betfair")),
        hours=float(sub.params.get("hours", 1.0)),
        min_margin_pct=float(sub.params.get("min_margin_pct", 1.0)),
        min_matched=float(sub.params.get("min_matched", 1000.0)),
        commission_pct=float(sub.params.get("commission_pct", 5.0)),
        limit=cap * 3, now=now)
    fired = 0
    for c in candidates:
        if fired >= cap:
            break
        lay_stake = c["lay_stake_per_dollar"] * bankroll
        profit = c["profit_pct"] / 100.0 * bankroll
        message = (
            f":lock: Back-Lay — {_sport_label(c['sport'])} — {c['fixture']}\n"
            f"Back {c['outcome']} at {c['book']} for {c['back_odds']:.2f} · "
            f"Lay on Betfair at {c['lay_odds']:.2f} "
            f"({_fmt_money(c['exchange_matched'])} matched)\n"
            f"Locked profit {c['profit_pct']:.2f} percent: on {_fmt_money(bankroll)} "
            f"back ${bankroll:.2f} and lay ${lay_stake:.2f} for ${profit:.2f} either way"
            f"{_age_label(c.get('seen'), now or dt.datetime.now(dt.UTC))}\n"
            f"_{c['note']}_"
        )
        bucket = int(c["profit_pct"] / 0.5)
        key = (f"back_lay:{c['fixture_id']}:{c['market']}:{c['outcome']}"
               f":{c['book']}:{bucket}")
        payload = {**c, "bankroll": bankroll}
        if await _fire(session, sub, kind="back_lay", key=key, message=message,
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
            f":crystal_ball: Prediction Market Value — {candidate['question']}\n"
            f"{candidate['back']} pays {candidate['back_odds']:.2f} on "
            f"{candidate['outcome']} against the {other} fair "
            f"{candidate['fair_odds']:.2f} · Edge +{candidate['edge_pct']:.1f} percent\n"
            f"Volume: Kalshi {_fmt_money(candidate.get('kalshi_volume'))} · "
            f"Polymarket {_fmt_money(candidate.get('polymarket_volume'))}\n"
            f"Suggested stake ${kelly:.2f} of {_fmt_money(bankroll)} bankroll — "
            f"confirm both platforms settle the question the same way"
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

    Reads structured stat lines from EVERY book whose captures carry
    player/stat/stat_line/line_type meta (Dabble natively; other books via the
    ingest prop tagger); O/U pairs at the same line de-vig into anchors that
    pin the fit's level. params.book narrows to one book; unset scans all.
    Ladders never mix books. Degrades cleanly with no engine configured."""
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

    book_filter = sub.params.get("book")  # None = every prop-tagged book
    min_edge = float(sub.params.get("min_edge_pct", 5.0))
    hours = float(sub.params.get("hours", 2.0))
    min_rungs = int(sub.params.get("min_rungs", 3))
    max_rmse = float(sub.params.get("max_rmse_log", 0.08))
    cap = int(sub.params.get("max_alerts_per_cycle", 5))

    from sqlalchemy import String, cast

    from sportsdata_agents.data.models import OddsSnapshot

    stmt = select(OddsSnapshot).where(
        OddsSnapshot.captured_at > now - dt.timedelta(hours=hours),
        # only prop-tagged rows leave the database — the all-books scan must
        # not hydrate every captured market to find the few ladders
        cast(OddsSnapshot.meta, String).like('%"player"%'),
    )
    if book_filter:
        stmt = stmt.where(OddsSnapshot.book == str(book_filter))
    rows = (await session.execute(stmt.order_by(OddsSnapshot.captured_at))).scalars().all()
    # latest quote per (book, event, player, stat, line, side) — one book's
    # ladder is internally consistent; a cross-book blend is not a ladder
    ladders: dict[tuple[str, str, str, str], dict[tuple[float, str], float]] = {}
    names: dict[str, str] = {}
    sports: dict[str, str] = {}
    for row in rows:
        meta = row.meta or {}
        player, stat, line = meta.get("player"), meta.get("stat"), meta.get("stat_line")
        side = str(meta.get("line_type", "")).lower()
        if not player or not stat or line is None or side not in ("over", "under"):
            continue
        key = (row.book, row.event_external_id, str(player), str(stat))
        ladders.setdefault(key, {})[(float(line), side)] = float(row.odds)
        names[row.event_external_id] = row.event_name
        sports[row.event_external_id] = row.sport

    fired = 0
    for (book, event_id, player, stat), quotes_by_rung in sorted(ladders.items()):
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
                f":dart: Player Prop Value — {_sport_label(sports.get(event_id, '?'))} — "
                f"{names.get(event_id, event_id)}\n"
                f"{book} · {player} · {stat} {side} {line}\n"
                f"Book price {odds:.2f} against the ladder-fitted fair "
                f"{fitted_fair:.2f} · Edge +{edge_pct:.1f} percent "
                f"(fitted from {len(seam_quotes)} rungs)"
            )
            bucket = int(edge_pct / 2.0)
            alert_key = f"stat_value:{book}:{event_id}:{player}:{stat}:{line}:{side}:{bucket}"
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
            # a small safety-lag on the cursor: ingest commits change-points
            # SECONDS after stamping changed_at, and ingest now runs concurrently
            # with monitor every 60s — a strict "> cursor" scan would skip rows
            # whose changed_at ≤ cursor but that committed after the last SELECT.
            # The _fire dedupe absorbs the small re-scan overlap.
            cursor = (sub.cursor - dt.timedelta(seconds=90)) if sub.cursor else now - dt.timedelta(hours=6)
            try:
                if sub.kind in ("line_move", "steam"):
                    # steam needs the FULL window_minutes to see min_moves moves —
                    # at a 60s monitor a key has ≤1 change-point since the cursor,
                    # so the cursor-window steam could never fire (dead watch).
                    if sub.kind == "steam":
                        window = float(sub.params.get("window_minutes", 30))
                        floor = now - dt.timedelta(minutes=window)
                    else:
                        floor = cursor
                    rows = list(
                        (
                            await session.execute(
                                select(Price).where(Price.changed_at > floor).order_by(Price.changed_at)
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
                elif sub.kind == "bsp_value":
                    fired = await _watch_bsp_value(session, sub, push, now=now)
                elif sub.kind == "back_lay":
                    fired = await _watch_back_lay(session, sub, push, now=now)
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
