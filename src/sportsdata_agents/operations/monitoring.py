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
import re
import uuid
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
    {book: best odds}. Matching is by MARKET FAMILY and numeric (side, line),
    not exact strings: books name the same market "spread"/"line"/"handicap"
    and stringify lines differently, so exact equality left every lined board
    reading NA while the whole industry priced the game. Side-relative
    selections (home/away, with or without a line) translate between books'
    listing orders; the handicap rides the team through the flip. When the
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
    side, line = _split_selection(row.selection.lower())
    family = _market_family(row.market)
    name_cache: dict[tuple[str, str], str] = {}
    own_name = await _event_name_for(session, name_cache, row.provider, row.event_external_id)
    side_relative = side in ("home", "away", "draw")
    if not side_relative and side not in ("over", "under"):
        mapped = _sel_team_side(side, own_name)  # "milwaukee +1.5" → away
        if mapped is not None:
            side, side_relative = mapped, True
    if side_relative and line is None and family == "line":
        line = _handicap_market_line(row.market.lower(), side, row.book)
    if line is None and family == "total" and side in ("over", "under"):
        line = _total_market_line(row.market.lower())
    own_seen = await _last_seen(session, row)
    quotes: dict[str, float] = {}
    for sibling in siblings:
        want_side = side
        if side_relative:
            sibling_name = await _event_name_for(session, name_cache, sibling.provider, sibling.external_id)
            translated = _translate_side(side, sibling_name, own_name)
            if translated is None:
                continue  # orientation unknown — never show a possibly-flipped price
            want_side = translated
        from sqlalchemy import or_

        stmt = (
            select(Price)
            .where(
                Price.provider == sibling.provider,
                Price.event_external_id == sibling.external_id,
            )
            .order_by(Price.changed_at.desc())
        )
        if family == "h2h":
            stmt = stmt.where(func.lower(Price.market).in_(_H2H_MARKETS)).limit(40)
        elif family == "total":
            stmt = stmt.where(or_(func.lower(Price.market).like("%total%"),
                                  func.lower(Price.market).like("%over/under%"),
                                  func.lower(Price.market).like("%u/o%"))).limit(120)
        elif family == "line":
            stmt = stmt.where(or_(func.lower(Price.market).like("%spread%"),
                                  func.lower(Price.market).like("%line%"),
                                  func.lower(Price.market).like("%handicap%"))).limit(120)
        else:
            stmt = stmt.where(Price.market == row.market).limit(30)
        for cand in (await session.execute(stmt)).scalars():
            if cand.book == row.book:
                continue
            if family is not None and _market_family(cand.market) != family:
                continue  # a period/segment variant slipping the SQL prefilter
            cand_side, cand_line = _split_selection(cand.selection.lower())
            if (side_relative and cand_side != want_side
                    and cand_side not in ("home", "away", "draw", "over", "under")):
                mapped = _sel_team_side(cand_side, sibling_name)
                if mapped is not None:
                    cand_side = mapped
            if cand_side != want_side:
                continue
            if cand_line is None and cand_side in ("home", "away"):
                cand_line = _handicap_market_line(cand.market.lower(), cand_side,
                                                  cand.book)
            if cand_line is None and cand_side in ("over", "under"):
                cand_line = _total_market_line(cand.market.lower())
            if (line is None) != (cand_line is None):
                continue
            if line is not None and cand_line is not None and abs(cand_line - line) > 1e-9:
                continue
            # rows arrive newest-first: the first match per book IS the current
            # price — never let an older, higher change-point overwrite it
            if not await _quote_still_listed(session, cand, own_seen):
                continue  # a renamed/delisted selection's last change-point
                # (lived: BetR's 09:52 "atlanta braves +1.5" shown at 19:00,
                # nine hours after the book renamed the selection to "away")
            quotes.setdefault(cand.book, float(cand.odds))
            break
    return quotes


async def _sibling_competition(
    session: AsyncSession, provider: str, event_id: str
) -> str:
    """The fixture's competition label from ANY sibling event's snapshots —
    most books never stamp a league, Pinnacle always does. Empty string when
    nobody knows (fail-open: an unknown league must not silence AFL alerts
    from books that don't stamp competitions)."""
    mapping = (await session.execute(
        select(Event).where(Event.provider == provider,
                            Event.external_id == event_id)
    )).scalars().first()
    if mapping is None or mapping.fixture_id is None:
        return ""
    siblings = (await session.execute(
        select(Event).where(Event.fixture_id == mapping.fixture_id,
                            Event.id != mapping.id)
    )).scalars().all()
    for sib in siblings:
        comp = (await session.execute(
            select(OddsSnapshot.meta["competition"].as_string()).where(
                OddsSnapshot.provider == sib.provider,
                OddsSnapshot.event_external_id == sib.external_id,
                OddsSnapshot.meta["competition"].as_string().isnot(None),
            ).order_by(OddsSnapshot.captured_at.desc()).limit(1)
        )).scalar()
        if comp:
            return str(comp)
    return ""


def _cooled_down(prev_at: dt.datetime, minutes: float = 20.0) -> bool:
    """A band-GROWTH re-fire waits out a cooldown: a drifting model fair
    walks a +2% boundary on every scan, and each crossing is technically
    'bigger' — without the floor one selection pinged three times in
    twelve minutes."""
    aware = prev_at if prev_at.tzinfo else prev_at.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - aware) >= dt.timedelta(minutes=minutes)


async def _last_seen(session: AsyncSession, row: Price) -> dt.datetime | None:
    """When this exact (provider, event, market, selection) was last captured
    in a snapshot (ix_snap_key_time serves it in full)."""
    seen = (await session.execute(
        select(func.max(OddsSnapshot.captured_at)).where(
            OddsSnapshot.provider == row.provider,
            OddsSnapshot.event_external_id == row.event_external_id,
            OddsSnapshot.market == row.market,
            OddsSnapshot.selection == row.selection,
        )
    )).scalar()
    if seen is None:
        return None
    return seen if seen.tzinfo else seen.replace(tzinfo=dt.UTC)


async def _quote_still_listed(
    session: AsyncSession, cand: Price, ref: dt.datetime | None
) -> bool:
    """Change-points persist forever, so a matching row proves the price
    EXISTED, not that it exists: a book that renames or delists a selection
    leaves its last change-point behind as a ghost. Live is judged against
    the BOOK'S OWN freshest capture of the event: a detail pass captures the
    whole board at once, so a selection lagging its event's newest sighting
    by more than a grace window was dropped from the board (lived: Dabble
    moved its total 10.5 -> 9.5 and 'under 10.5' alerted 40 minutes dead —
    an absolute window can't fit both Dabble's 5-minute and BetR's hourly
    capture cadences). A 3h absolute ceiling vs `ref` stays as the belt.
    NO snapshot at all reads as live: the store writes a snapshot beside
    every change-point, so a ghost always has an OLD sighting, never a
    missing one."""
    seen = await _last_seen(session, cand)
    if seen is None:
        return True
    anchor = ref or dt.datetime.now(dt.UTC)
    if (anchor - seen) > dt.timedelta(hours=3):
        return False
    event_seen = (await session.execute(
        select(func.max(OddsSnapshot.captured_at)).where(
            OddsSnapshot.provider == cand.provider,
            OddsSnapshot.event_external_id == cand.event_external_id,
        )
    )).scalar()
    if event_seen is None:
        return True
    event_aware = (event_seen if event_seen.tzinfo
                   else event_seen.replace(tzinfo=dt.UTC))
    return seen >= event_aware - dt.timedelta(minutes=30)


async def _nearest_line_quotes(
    session: AsyncSession, row: Price, line: float
) -> tuple[float, dict[str, float]] | None:
    """When no other book quotes the EXACT line (alt-ladder rungs are often
    one book's private product), the industry's nearest quoted line is the
    context a human wants — returned as (nearest_line, {book: odds}) for the
    same side, or None when the fixture is unresolved or nothing is close."""
    from sqlalchemy import or_

    from sportsdata_agents.quant.backtest import _event_name_for, _translate_side

    mapping = (await session.execute(
        select(Event).where(Event.provider == row.provider,
                            Event.external_id == row.event_external_id)
    )).scalars().first()
    if mapping is None or mapping.fixture_id is None:
        return None
    siblings = (await session.execute(
        select(Event).where(Event.fixture_id == mapping.fixture_id,
                            Event.id != mapping.id)
    )).scalars().all()
    side, _ = _split_selection(row.selection.lower())
    family = _market_family(row.market)
    if not siblings or family not in ("total", "line"):
        return None
    name_cache: dict[tuple[str, str], str] = {}
    own_name = await _event_name_for(session, name_cache, row.provider, row.event_external_id)
    side_relative = side in ("home", "away", "draw")
    if not side_relative and side not in ("over", "under"):
        mapped = _sel_team_side(side, own_name)  # TAB's "milwaukee +1.5"
        if mapped is not None:
            side, side_relative = mapped, True
    own_seen = await _last_seen(session, row)
    by_line: dict[float, dict[str, float]] = {}
    for sibling in siblings:
        want_side = side
        if side_relative:
            sibling_name = await _event_name_for(session, name_cache, sibling.provider, sibling.external_id)
            translated = _translate_side(side, sibling_name, own_name)
            if translated is None:
                continue
            want_side = translated
        tokens = (("%total%", "%over/under%", "%u/o%") if family == "total"
                  else ("%spread%", "%line%", "%handicap%"))
        stmt = (select(Price)
                .where(Price.provider == sibling.provider,
                       Price.event_external_id == sibling.external_id,
                       or_(*[func.lower(Price.market).like(t) for t in tokens]))
                .order_by(Price.changed_at.desc()).limit(120))
        seen: set[tuple[str, float]] = set()
        for cand in (await session.execute(stmt)).scalars():
            if cand.book == row.book or _market_family(cand.market) != family:
                continue
            cand_side, cand_line = _split_selection(cand.selection.lower())
            if (side_relative and cand_side != want_side
                    and cand_side not in ("home", "away", "draw", "over", "under")):
                mapped = _sel_team_side(cand_side, sibling_name)
                if mapped is not None:
                    cand_side = mapped
            if cand_side != want_side or cand_line is None:
                continue
            if abs(cand_line - line) < 1e-9 or (cand.book, cand_line) in seen:
                continue  # the exact line is the board's job; newest-first per key
            seen.add((cand.book, cand_line))
            if not await _quote_still_listed(session, cand, own_seen):
                continue  # ghost change-point of a renamed/delisted selection
            by_line.setdefault(cand_line, {}).setdefault(cand.book, float(cand.odds))
    if not by_line:
        return None
    nearest = min(by_line, key=lambda ln: abs(ln - line))
    return nearest, by_line[nearest]


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
    side = row.selection.split()[0].lower() if row.selection else ""
    if who and side not in ("over", "under") and str(who).strip().lower() != row.selection.lower():
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

# novelty-product "runners" that ride a race's own event key (Entain quotes
# Odds vs Evens, Favourite vs Field as win-market rows) — never real horses
_NON_RUNNERS = frozenset({"odds", "evens", "favourite", "favourites", "favorite",
                          "field", "the field", "any other", "any other runner"})


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
    if race:
        # sponsor-named cards (Dabble) carry no R<n>: the FULL label is the
        # race identity — a bare venue merged every race at the meeting into
        # one dedupe key and one garbage story
        return f"{venue} · {race.strip()}"
    return base.strip()


async def _racing_board(session: AsyncSession, event_name: str, market: str,
                        selection: str, book: str,
                        start: dt.datetime | None = None) -> tuple[dict[str, float], set[str]]:
    """Other books' latest price for the same runner — racing events don't map
    through the fixture resolver; the join is the venue token + R<n> tag
    ("MANAWATU R1" / "Manawatu R1" / "Mountaineer Park R3" all match). Rows
    WITHOUT a race tag (Dabble's sponsor-named cards) join when their start
    time sits within 5 minutes of ``start``.

    Returns (quotes, thin) — thin names exchange rows with under $500 matched.

    Runners are keyed TWO ways across the industry — saddle number (FanDuel,
    Ladbrokes, PointsBet, BetR, Sportsbet, TAB) versus runner name (Betfair,
    Unibet) — so the board translates through the number↔name bridge built
    from the number-keyed rows' own runner meta. Selection string equality
    alone left half the industry NA on fully priced races."""
    if not event_name:
        return {}, set()
    import re as _re

    from sqlalchemy import func

    race_match = _re.search(r"\bR(\d+)\b", event_name, _re.IGNORECASE)
    venue_token = event_name.split()[0].lower() if event_name.split() else ""
    if not race_match or len(venue_token) < 3:
        return {}, set()
    race_tag = f"r{race_match.group(1)}"
    rows = (await session.execute(
        select(OddsSnapshot.book, OddsSnapshot.odds, OddsSnapshot.selection,
               OddsSnapshot.event_name, OddsSnapshot.meta, OddsSnapshot.start_time)
        .where(func.lower(OddsSnapshot.event_name).like(f"{venue_token}%"),
               OddsSnapshot.market == market,
               OddsSnapshot.captured_at > dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30))
        .order_by(OddsSnapshot.captured_at.desc()).limit(800)
    )).all()
    # race-scoped rows, newest first per (book, selection)
    race_rows: list[tuple[str, float, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for other_book, odds, sel, name, meta, row_start in rows:
        tags = {w.lower() for w in _re.findall(r"\bR\d+\b", name or "", _re.IGNORECASE)}
        if race_tag not in tags:
            # sponsor-named cards carry no race tag — the start time is the
            # race identity (5 min covers books' post-time disagreements)
            if start is None or row_start is None:
                continue
            when = row_start if row_start.tzinfo else row_start.replace(tzinfo=dt.UTC)
            anchor = start if start.tzinfo else start.replace(tzinfo=dt.UTC)
            if abs((when - anchor).total_seconds()) > 300:
                continue
        if (other_book, str(sel)) in seen:
            continue
        if str((meta or {}).get("runner") or "").strip().lower() in _NON_RUNNERS:
            continue  # novelty products (Odds vs Evens) ride the same race key —
            # they must not reach the board OR the number↔name bridge below
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
    thin: set[str] = set()
    for other_book, odds, sel, meta in race_rows:
        if other_book == book:
            continue
        sel_l = sel.lower()
        runner = str(meta.get("runner") or "").strip().lower()
        # when the row NAMES its runner and we know ours, the names must agree —
        # saddle-number equality alone joined a novelty product's "runner 2"
        # (Evens at 2.60) onto a 21.00 horse's board (lived: Menangle R1)
        is_ours = (runner == name if runner and name
                   else bool((number and sel_l == number) or (name and sel_l == name)))
        if is_ours and other_book not in quotes:
            quotes[other_book] = odds
            matched = meta.get("total_matched")
            try:
                # a near-untraded exchange price is takeable but weak — show
                # it TAGGED rather than hiding it (a hidden Betfair read as
                # "no other book has priced this")
                if matched is not None and float(matched) < 500.0:
                    thin.add(other_book)
            except (TypeError, ValueError):
                pass
    return quotes, thin


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
    # prediction venues price their own question universe — on a book board
    # they can only ever read NA
    pack = tuple(sorted(b for b in rows if b not in ("Kalshi", "Polymarket")))
    _PACK_CACHE[sport] = (time.monotonic(), pack)
    return pack


def _format_board(quotes: dict[str, float], sharps: list[str],
                  include: tuple[str, ...] = (), *,
                  thin: set[str] | frozenset[str] = frozenset(),
                  engine_fair: float | None = None,
                  subject: str = "") -> str:
    """The industry board: ENGINE FAIR first, then sharps, then every book
    that has actually PRICED it, highest price bolded. Books without a price
    are a single count at the end, never listed — walls of "NA" drowned the
    one real price (lived: a US harness board read eight NAs and one quote).
    Exchange rows with little matched money read "thin" instead of hiding.
    ``subject`` names WHOSE board it is ("Tunbridge · win") — a multi-runner
    alert's board otherwise reads as anyone's."""
    ordered: list[tuple[str, float]] = []
    seen: set[str] = set()
    for book in sharps:
        if book in quotes and book not in seen:
            ordered.append((book, quotes[book]))
            seen.add(book)
    for book, odds in sorted(quotes.items(), key=lambda kv: -kv[1]):
        if book not in seen:
            ordered.append((book, odds))
            seen.add(book)
    best = max((o for _b, o in ordered[:12]), default=None)

    def cell(book: str, odds: float) -> str:
        text = f"{book} {odds:.2f}" + (" thin" if book in thin else "")
        return f"**{text}**" if odds == best else text

    engine_cell = f"Engine {engine_fair:.2f} · " if engine_fair else ""
    head = f"across books — {subject}:" if subject else "across books:"
    if not ordered:
        return (f"\n{head} Engine {engine_fair:.2f} — no book board "
                f"captured yet" if engine_fair else "")
    board = " · ".join(cell(b, o) for b, o in ordered[:12])
    unpriced = sum(1 for b in {*sharps, *include} if b not in quotes)
    if len(ordered) <= 1 and unpriced:
        # alt lines are often one book's private rung — say so instead of
        # counting NAs that could never be anything else
        tail = " · no other book prices this"
    else:
        tail = (f" · {unpriced} {'book' if unpriced == 1 else 'books'} NA"
                if unpriced else "")
    return f"\n{head} {engine_cell}{board}{tail}"


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


def _lacks_clear_ev(sub: Subscription, odds: float, engine_fair: float | None,
                    quotes: dict[str, float] | None = None) -> bool:
    """True = suppress: the watch demands DEMONSTRATED value
    (min_engine_edge_pct) and this price shows none — the engine fair must
    beat the price by the floor, OR an exchange (Betfair; FanDuel's tote pool
    counts) must quote UNDER the price, corroborating that money agrees."""
    floor = sub.params.get("min_engine_edge_pct")
    if floor is None:
        return False
    engine_ok = (engine_fair is not None and engine_fair > 0
                 and (float(odds) / engine_fair - 1.0) * 100.0 >= float(floor))
    corroborators = [str(b) for b in sub.params.get(
        "exchange_corroborators", ["Betfair", "FanDuel"])]
    exchange_ok = any((quotes or {}).get(b, float("inf")) < float(odds)
                      for b in corroborators)
    return not (engine_ok or exchange_ok)


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
    alert = Alert(
        tenant_id=subscription.tenant_id, workspace_id=subscription.workspace_id,
        subscription_id=subscription.id, kind=kind, message=message,
        payload=payload, dedupe_key=key, pushed=False,
    )
    # RECORD BEFORE PUSH, committed: a pass killed mid-flight used to roll the
    # rows back AFTER the webhooks had fired — dedupe never learned the alert
    # existed and every retry pass re-pushed the same story (lived: duplicate
    # Discord messages with no matching alert rows, 2026-07-07)
    session.add(alert)
    await session.commit()
    if float(subscription.params.get("digest_hours", 0) or 0) > 0 or _in_quiet_hours(subscription):
        return True  # digest watches batch pushes; quiet hours keep the
        # phone silent overnight — either way the alert ROW is the record
    try:
        alert.pushed = await pusher(subscription, message)
    except Exception as e:  # a push failure must not sink the watch — the row is the record
        logger.warning("push failed for %s: %s", subscription.name, e)
        alert.pushed = False
    await session.commit()
    return True


def _racing_too_early(sub: Subscription, ctx: dict[str, Any]) -> bool:
    """True = suppress: a racing move HOURS before the jump — most books
    haven't opened or been captured yet, so there is nothing to compare and
    the interesting move is the one near post time anyway."""
    lead_cap = sub.params.get("racing_max_lead_minutes")
    if not lead_cap or ctx.get("sport") not in _RACING_LABELS:
        return False
    start = ctx.get("start_time")
    if start is None or _started(start):
        return False
    when = start if start.tzinfo else start.replace(tzinfo=dt.UTC)
    return when - dt.datetime.now(dt.UTC) > dt.timedelta(minutes=float(lead_cap))


def _edge_stake_note(sub: Subscription, odds: float, engine_fair: float | None,
                     *, compact: bool = False) -> str:
    """Stake sizing on move alerts — only when the engine actually sees value
    at this price (kelly needs an opinion; without a fair there is no stake)."""
    if engine_fair is None or engine_fair <= 1.0 or odds <= engine_fair:
        return ""
    bankroll = float(sub.params.get("bankroll", 100.0))
    kelly = _kelly_stake(1.0 / engine_fair, odds, bankroll)
    if compact:
        return f" · stake {_fmt_money(kelly)}"
    return (f"\nSuggested stake {_fmt_money(kelly)} of {_fmt_money(bankroll)} "
            f"bankroll — the price beats the engine fair")


async def _move_form_note(session: AsyncSession, ctx: dict[str, Any], selection: Any) -> str:
    """The alerted runner's form line on racing move alerts (the form store
    keys runners by saddle number, so number selections only)."""
    if ctx.get("sport") not in _RACING_LABELS or not ctx.get("start_time"):
        return ""
    if not str(selection).strip().isdigit():
        return ""
    import re as _re

    race = _re.search(r"\bR(\d+)\b", str(ctx["event"]), _re.IGNORECASE)
    if not race:
        return ""
    return await _runner_form_note(session, race.group(1),
                                   ctx["start_time"].isoformat(), str(selection).strip())


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
        if _racing_too_early(sub, ctx):
            continue
        if _thin_exchange(sub, ctx):
            continue
        engine_fair = await _engine_fair_for(
            session, row.market, row.selection, event_id=row.event_external_id)
        quotes = await _cross_book_quotes(session, row)
        thin: set[str] = set()
        if not quotes and ctx["sport"] in _RACING_LABELS:
            quotes, thin = await _racing_board(session, _racing_board_key(str(ctx["event"])),
                                               row.market, row.selection, row.book,
                                               start=ctx.get("start_time"))
        if _exchange_alone(sub, ctx, quotes):
            continue
        if _engine_veto(sub, float(row.odds), engine_fair, quotes):
            continue
        if _lacks_clear_ev(sub, float(row.odds), engine_fair, quotes):
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
                             "engine_fair": engine_fair, "quotes": quotes,
                             "thin": thin}
    # one MESSAGE per event+market+direction: three runners drifting in one
    # race is one story, not three pings (fewer messages, more information)
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (event_key, market, _sel_key, direction), group in groups.items():
        by_event.setdefault((event_key, market, direction), []).append(group)
    fired = 0
    for (event_key, market, direction), sel_groups in by_event.items():
        if fired >= cap:
            break
        sel_groups.sort(key=lambda g: -g["best"]["move"])
        top = sel_groups[0]["best"]
        ctx, quotes = top["ctx"], top["quotes"]
        direction_word = "shortened" if direction == "shortened" else "drifted out"
        multi = len(sel_groups) > 1
        lines = []
        for group in sel_groups[:4]:
            best = group["best"]
            movers = " · ".join(
                f"{b} {prev:.2f} to {new:.2f}"
                for b, (prev, new, _m) in sorted(group["movers"].items(),
                                                 key=lambda kv: -kv[1][2]))
            fair = (f" · fair {best['engine_fair']:.2f}"
                    if multi and best["engine_fair"] else "")
            stake = (_edge_stake_note(sub, float(best["row"].odds),
                                      best["engine_fair"], compact=True)
                     if multi else "")
            lines.append(f"**{best['ctx']['selection']}**: {movers} "
                         f"({best['move']:.1f} percent){fair}{stake}")
        if len(sel_groups) > 4:
            lines.append(f"…and {len(sel_groups) - 4} more selections moved")
        money_word = "pool" if (ctx.get("money_kind") == "pool") else "matched"
        traded = (f" · {_fmt_money(float(ctx['matched']))} {money_word}"
                  if ctx.get("matched") else "")
        jump = ""
        if ctx.get("start_time") and not _started(ctx["start_time"]):
            jump = f" · Starts {_local_hhmm(ctx['start_time'].isoformat(), _tz_for(sub))}"
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        include = await _coverage_pack(session, str(ctx["sport"]))
        for mover, (_prev, new, _m) in sel_groups[0]["movers"].items():
            quotes.setdefault(mover, new)  # every mover is a price, not NA
        stake_note = "" if multi else _edge_stake_note(
            sub, float(top["row"].odds), top["engine_fair"])
        form_note = await _move_form_note(session, ctx, top["row"].selection)
        message = (
            f":chart_with_upwards_trend: Line Move — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
            f"Market: {market} · Price {direction_word}{traded}{jump}\n"
            + "\n".join(lines)
            + stake_note + form_note
            + _format_board(quotes, sharps, include, thin=top.get("thin") or set(),
                            engine_fair=top["engine_fair"],
                            subject=f"{top['ctx']['selection']} · {market}")
        )
        key = f"line_move:{event_key}:{market}:{direction}"
        payload = {"move_pct": round(top["move"], 2),
                   "selections": [g["best"]["row"].selection for g in sel_groups],
                   "books": sorted({b for g in sel_groups for b in g["movers"]})}
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
        if _racing_too_early(sub, ctx):
            continue
        if _thin_exchange(sub, ctx):
            continue
        arrow = "drifting" if net == 1 else "steaming in"
        engine_fair = await _engine_fair_for(session, market, selection, event_id=event)
        quotes = await _cross_book_quotes(session, series[-1])
        thin: set[str] = set()
        if not quotes and ctx["sport"] in _RACING_LABELS:
            quotes, thin = await _racing_board(session, _racing_board_key(str(ctx["event"])),
                                               market, selection, book,
                                               start=ctx.get("start_time"))
        current = float(series[-1].odds)
        if _exchange_alone(sub, ctx, quotes):
            continue
        if _engine_veto(sub, current, engine_fair, quotes):
            continue
        if _lacks_clear_ev(sub, current, engine_fair, quotes):
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
                             "engine_fair": engine_fair, "quotes": quotes,
                             "thin": thin}
    # one MESSAGE per event+market+direction (fewer messages, more information)
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (event_key, market, _sel_key, arrow), group in groups.items():
        by_event.setdefault((event_key, market, arrow), []).append(group)
    fired = 0
    for (event_key, market, arrow), sel_groups in by_event.items():
        if fired >= cap:
            break
        sel_groups.sort(key=lambda g: -g["best"]["moves"])
        top = sel_groups[0]["best"]
        ctx, quotes = top["ctx"], top["quotes"]
        direction_word = "drifting out" if arrow == "drifting" else "steaming in"
        multi = len(sel_groups) > 1
        lines = []
        for group in sel_groups[:4]:
            best = group["best"]
            streaks = " · ".join(
                f"{b} {prev:.2f} to {new:.2f} over {n} moves"
                for b, (prev, new, n) in sorted(group["streaks"].items(),
                                                key=lambda kv: -kv[1][2]))
            fair = (f" · fair {best['engine_fair']:.2f}"
                    if multi and best["engine_fair"] else "")
            stake = (_edge_stake_note(sub, float(best["row"].odds),
                                      best["engine_fair"], compact=True)
                     if multi else "")
            lines.append(f"**{best['ctx']['selection']}**: {streaks}{fair}{stake}")
        if len(sel_groups) > 4:
            lines.append(f"…and {len(sel_groups) - 4} more selections walking")
        money_word = "pool" if (ctx.get("money_kind") == "pool") else "matched"
        traded = (f" · {_fmt_money(float(ctx['matched']))} {money_word}"
                  if ctx.get("matched") else "")
        jump = ""
        if ctx.get("start_time") and not _started(ctx["start_time"]):
            jump = f" · Starts {_local_hhmm(ctx['start_time'].isoformat(), _tz_for(sub))}"
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        include = await _coverage_pack(session, str(ctx["sport"]))
        for mover, (_prev, new, _n) in sel_groups[0]["streaks"].items():
            quotes.setdefault(mover, new)  # every walking book is a price, not NA
        stake_note = "" if multi else _edge_stake_note(
            sub, float(top["row"].odds), top["engine_fair"])
        form_note = await _move_form_note(session, ctx, top["row"].selection)
        message = (
            f":fire: Steam — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
            f"Market: {market} · Price {direction_word}{traded}{jump}\n"
            + "\n".join(lines)
            + stake_note + form_note
            + _format_board(quotes, sharps, include, thin=top.get("thin") or set(),
                            engine_fair=top["engine_fair"],
                            subject=f"{top['ctx']['selection']} · {market}")
        )
        # band the dedupe by streak length: fires at min_moves, again each
        # time the longest streak doubles — a runaway steam keeps reporting
        band = top["moves"] // max(min_moves, 1)
        key = f"steam:{event_key}:{market}:{arrow}:{band}"
        payload = {"moves": top["moves"],
                   "selections": [g["best"]["row"].selection for g in sel_groups],
                   "books": sorted({b for g in sel_groups for b in g["streaks"]})}
        if await _fire(session, sub, kind="steam", key=key, message=message,
                       payload=payload, pusher=pusher):
            fired += 1
    return fired


async def _watch_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *,
    now: dt.datetime | None = None,
) -> int:
    """Recorded model edge at the LATEST price — one message per MARKET story.

    Every selection whose edge crosses ``min_edge_pct`` on the same
    event+market lands in ONE message (model fair, book price, stake, the
    industry board), mirroring model_value's grouping. Predictions must be
    FRESH (the slate re-records every pass; a stale fair is not a fair — an
    un-timestamped row is accepted) and the event pre-match — the recorded
    probabilities do not know the score."""
    from sqlalchemy import or_

    now = now or dt.datetime.now(dt.UTC)
    min_edge = float(sub.params.get("min_edge_pct", 3.0))
    cap = int(sub.params.get("max_alerts_per_cycle", 10))
    bankroll = float(sub.params.get("bankroll", 100.0))
    max_pred_age = dt.timedelta(hours=float(sub.params.get("max_prediction_age_hours", 6.0)))
    stmt = (
        select(Prediction).where(
            Prediction.tenant_id == sub.tenant_id,
            Prediction.workspace_id == sub.workspace_id,
            or_(Prediction.predicted_at.is_(None),
                Prediction.predicted_at > now - max_pred_age),
        ).order_by(Prediction.predicted_at.desc().nulls_last())
    )
    model_prefix = sub.params.get("model")
    excluded_engine = {str(x) for x in (sub.params.get("exclude_engine_sports") or ())}
    if model_prefix or excluded_engine:
        # one fair-price FAMILY per watch: the anchored slate and the ratings
        # slate record the same (event, market, selection) keys, and without
        # this filter whichever recorded most recently silently wins. Engine
        # sports excluded HERE, not after the price lookups — engine:racing
        # alone records 12k+ predictions a day, and fetching prices for keys
        # a sport filter would drop wedged the whole monitor pass (rc=-1
        # timeouts, every alert kind silent; lived: 2026-07-07 evening)
        from sportsdata_agents.data.models import ModelArtifact

        stmt = stmt.join(ModelArtifact, ModelArtifact.id == Prediction.model_id)
        if model_prefix:
            stmt = stmt.where(
                ModelArtifact.name.startswith(str(model_prefix), autoescape=True))
        for engine_sport in excluded_engine:
            stmt = stmt.where(~ModelArtifact.name.endswith(f":{engine_sport}"))
    predictions = (await session.execute(stmt)).scalars().all()
    # newest prediction per (event, market, selection) — the slate re-records
    # every pass, and yesterday's probability is not an opinion on today's price
    newest: dict[tuple[str, str, str, str], Prediction] = {}
    for pred in predictions:
        newest.setdefault((pred.provider, pred.event_external_id,
                           pred.market, pred.selection), pred)
    # batched lookup PER EVENT for the latest price per key — a query per
    # KEY wedged the pass once a big model family was in scope, and a
    # tuple-IN over thousands of keys sequential-scans the whole prices
    # table (no composite index); per-event queries ride the event index
    # and events number in the dozens
    # PROVIDER-SCOPED: numeric event ids collide across feeds (Sportsbet,
    # Unibet and BetR all mint 7-8 digit ints) — an unscoped lookup can pair
    # a prediction with another feed's game. Predictions carry the BOOK
    # label; price rows carry both the label and the feed.
    latest_by_key: dict[tuple[str, str, str, str], Price] = {}
    feed_of: dict[str, str] = {}  # event id -> its own feed, per price rows
    scopes = {(k[0], k[1]) for k in newest}
    for book_label, event_id in scopes:
        price_stmt = select(Price).where(
            Price.event_external_id == event_id,
            Price.changed_at > now - dt.timedelta(hours=48),
        )
        if book_label:
            # prediction writers stamp either the BOOK label ("Pinnacle",
            # the slates) or the FEED ("nba_cdn", calibrations) — accept both
            price_stmt = price_stmt.where(or_(Price.book == book_label,
                                              Price.provider == book_label))
        rows = (await session.execute(
            price_stmt.order_by(Price.changed_at.desc()))).scalars().all()
        for row in rows:
            feed_of.setdefault(row.event_external_id, row.provider)
            latest_by_key.setdefault(
                (book_label, row.event_external_id, row.market, row.selection),
                row)
    # fold sibling events onto their FIXTURE so one game is ONE story — the
    # slates record predictions against several books' event ids, and
    # event-keyed stories alerted the same match once per book (lived:
    # Fremantle v Sydney pushed twice, TAB-keyed and Pinnacle-keyed)
    fixture_of: dict[str, str] = {}
    pred_event_ids = {k[1] for k in newest}
    if pred_event_ids:
        for ev in (await session.execute(
                select(Event).where(Event.external_id.in_(pred_event_ids))
        )).scalars():
            feed = feed_of.get(ev.external_id)
            if ev.fixture_id is not None and feed in (None, ev.provider):
                fixture_of.setdefault(ev.external_id, str(ev.fixture_id))
    stories: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (book_label, event_id, market, _sel), pred in newest.items():
        latest = latest_by_key.get((book_label, event_id, market, _sel))
        if latest is None or not _match(latest, sub.params):
            continue
        edge = (float(pred.prob) * float(latest.odds) - 1.0) * 100.0
        stories.setdefault((fixture_of.get(event_id, event_id), market), []).append(
            {"pred": pred, "row": latest, "edge": edge})
    fired = 0
    ordered = sorted(stories.items(), key=lambda kv: -max(e["edge"] for e in kv[1]))
    for (event_id, market), entries in ordered:
        if fired >= cap:
            break
        live = sorted((e for e in entries if e["edge"] >= min_edge),
                      key=lambda e: -e["edge"])
        # LIVENESS: drop entries whose exact selection wasn't captured in a
        # recent snapshot — a delisted alt-line rung keeps its change-point
        # forever and reads as a phantom edge (same class as the boards fix)
        checked = []
        for e in live:
            if await _quote_still_listed(session, e["row"], now):
                checked.append(e)
        live = checked
        # the previously-alerted lookup matches ANY band of this market
        # (startswith, autoescaped so ids with `_`/`%` don't act as wildcards)
        base_key = f"value:{event_id}:{market}"
        previously = (
            await session.execute(
                select(Alert)
                .where(Alert.subscription_id == sub.id,
                       or_(Alert.dedupe_key.startswith(f"{base_key}:", autoescape=True),
                           Alert.dedupe_key == f"vanished:{base_key}"),
                       Alert.kind.in_(("value", "value_vanished")))
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if not live:
            if (previously is not None and previously.kind == "value"
                    and previously.payload.get("edge_pct", 0) > 0):
                display = (await _event_display_name(session, event_id) or event_id)
                best = max(e["edge"] for e in entries)
                message = (
                    f":hourglass: Edge Gone — {display}\n"
                    f"Market: {market} — the best edge is now +{best:.1f} percent, "
                    f"under the {min_edge:g} percent floor"
                )
                if await _fire(session, sub, kind="value_vanished",
                               key=f"vanished:{base_key}", message=message,
                               payload={"edge_pct": round(best, 2)}, pusher=pusher):
                    fired += 1
            continue
        top = live[0]
        # band suppression FIRST — a persistent same-band edge is the steady
        # state, and this loop is the hottest in the stack: skip before the
        # context/board/coverage queries, not after assembling the message
        band = int(max(top["edge"], 0.0) / 2.0)  # re-fire each +2% the edge grows
        if previously is not None and previously.kind == "value":
            prior_band = int(max(float(previously.payload.get("edge_pct", 0.0)),
                                 0.0) / 2.0)
            if band <= prior_band:
                continue  # an edge drifting back DOWN a band is the same
                # opportunity shrinking, not news (after a vanish it re-fires)
            if not _cooled_down(previously.created_at):
                continue  # growing, but a drifting fair walks a band boundary
                # every scan — three pings in twelve minutes is spam (lived)
        ctx = await _context(session, top["row"])
        if bool(sub.params.get("pre_match_only", True)) and _started(ctx["start_time"]):
            continue  # recorded fairs do not know the live score
        quotes = await _cross_book_quotes(session, top["row"])
        for e in live:
            quotes.setdefault(e["row"].book, float(e["row"].odds))
        include = await _coverage_pack(session, str(ctx["sport"]))
        lines = []
        for e in live[:4]:
            prob = float(e["pred"].prob)
            kelly = _kelly_stake(prob, float(e["row"].odds), bankroll)
            lines.append(
                f"**{e['pred'].selection}**: {e['row'].book} {float(e['row'].odds):.2f} "
                f"· model {prob:.0%} (fair {1.0 / prob:.2f}) · edge +{e['edge']:.1f} "
                f"percent · stake {_fmt_money(kelly)}")
        if len(live) > 4:
            lines.append(f"…and {len(live) - 4} more selections with edge")
        jump = ""
        if ctx.get("start_time") and not _started(ctx["start_time"]):
            jump = f" · Starts {_local_hhmm(ctx['start_time'].isoformat(), _tz_for(sub))}"
        family = ""
        if model_prefix:
            family = (" · stats model (no market input)"
                      if "ratings" in str(model_prefix) or "form" in str(model_prefix)
                      else " · calibrated model")
        message = (
            f":moneybag: Model Edge — {_sport_label(ctx['sport'])} — {ctx['event']}\n"
            f"Market: {market} · Edge +{top['edge']:.1f} percent{family}{jump}\n"
            + "\n".join(lines)
            + f"\n{_fmt_money(bankroll)} bankroll"
            + _format_board(quotes, ["Pinnacle", "Betfair"], include,
                            subject=f"{top['pred'].selection} · {market}")
        )
        payload = {
            # top-of-story fields keep the outcome re-measurement loop working
            "edge_pct": round(top["edge"], 2), "prob": float(top["pred"].prob),
            "min_edge_pct": min_edge, "provider": top["row"].provider,
            "book": top["row"].book,
            "event_external_id": top["row"].event_external_id,
            "market": market, "selection": top["pred"].selection,
            "selections": [{"selection": e["pred"].selection, "book": e["row"].book,
                            "odds": float(e["row"].odds),
                            "edge_pct": round(e["edge"], 2)} for e in live[:12]],
        }
        if await _fire(session, sub, kind="value", key=f"{base_key}:{band}",
                       message=message, payload=payload, pusher=pusher):
            fired += 1
    return fired


_UNIT_WORDS = frozenset({"run", "runs", "point", "points", "goal", "goals",
                         "game", "games", "set", "sets", "map", "maps"})


def _split_selection(selection: str) -> tuple[str, float | None]:
    """Normalised selections embed lines as a trailing number: ``home -1.5``,
    ``over 220.5`` → (side, line); plain sides/runners come back line-less.
    A unit word after the number ("over 7.5 runs" — TAB) is part of the line,
    not the side: it left TAB off every total board it priced."""
    head, _, tail = selection.rpartition(" ")
    if head:
        try:
            return head, float(tail)
        except ValueError:
            if tail in _UNIT_WORDS:
                head2, _, tail2 = head.rpartition(" ")
                if head2:
                    try:
                        return head2, float(tail2)
                    except ValueError:
                        pass
    return selection, None


_MARKET_LINE_PAREN = re.compile(r"\(([+-]?\d+(?:\.\d+)?)\)")

# books whose parenthesised-handicap SIGN FRAME has been verified against a
# lined book on shared fixtures. The frame is per-book editorial choice: an
# unverified book's "handicap (2.5)" could be the away line, and a wrong
# sign shows a flipped price — worse than an absent one. Verify, then add.
_PAREN_HANDICAP_BOOKS = frozenset({"dabble"})


def _handicap_market_line(market: str, side: str, book: str) -> float | None:
    """Books like Dabble put the handicap in the MARKET label with bare-side
    selections: "handicap (-1.5)" / home. The parenthesised number is the
    HOME team's line; away takes the negation (verified against Unibet's
    lined selections on the same fixtures). Verified books only — other
    conventions ("run line +1.5") stay unjoined."""
    if book.lower() not in _PAREN_HANDICAP_BOOKS:
        return None
    m = _MARKET_LINE_PAREN.search(market)
    if m is None:
        return None
    val = float(m.group(1))
    return val if side == "home" else -val


def _total_market_line(market: str) -> float | None:
    """A total whose line lives in the MARKET label with bare over/under
    selections: "total points o/u (46.5)" / over. Unlike handicaps there is
    no sign frame to get wrong — and WITHOUT extraction two such books at
    DIFFERENT totals both read (over, None) and join as one market."""
    m = _MARKET_LINE_PAREN.search(market)
    return float(m.group(1)) if m else None


def _sel_team_side(sel_head: str, event_name: str) -> str | None:
    """A TEAM-named selection ("milwaukee", "nsw blues", "maroons") → home/away
    in its own book's frame. TAB and Dabble print names where the sink couldn't
    normalise a side, and exact-string matching left them off every board.
    Matches only when the name points at exactly ONE side of the event."""
    from sportsdata_agents.operations.resolution.resolver import _side_ok, _token_match, _tokens, split_sides

    sides = split_sides(event_name or "")
    if not sides:
        return None
    sel = _tokens(sel_head)
    if not sel:
        return None

    def hits(side_name: str) -> bool:
        side_toks = _tokens(side_name)
        if not side_toks:
            return False
        if _side_ok(sel, side_toks):
            return True
        # nickname riders ("nsw blues"): any strong token or the side's
        # initials counts — ambiguity is rejected below, not here
        initials = "".join(sorted(u[0] for u in side_toks))
        for t in sel:
            if (len(side_toks) >= 2 and len(t) == len(side_toks)
                    and "".join(sorted(t)) == initials):
                return True
            if len(t) >= 4 and any(_token_match(t, u) for u in side_toks):
                return True
        return False

    home, away = hits(sides[0]), hits(sides[1])
    if home == away:  # neither or both — never guess
        return None
    return "home" if home else "away"


_H2H_MARKETS = {"2way", "h2h", "head_to_head", "match_winner", "money line", "moneyline"}
_TOTAL_MARKETS = {"total", "totals"}
_LINE_MARKETS = {"spread", "line", "handicap"}

# period/segment/derived variants are DIFFERENT markets — a "spread p1" or
# "run line - after 5 innings" must never join a full-game board
_SEGMENT_MARKERS = ("p1", "p2", "p3", "1st", "2nd", "3rd", "4th", "after",
                    "inning", "half", "quarter", "period", "q1", "q2", "q3",
                    "q4", "set", "frame", "double", "odd", "team", "first",
                    "second", "race to", "margin", "alt.")


def _market_family(market: str) -> str | None:
    """h2h/total/line for a FULL-GAME market label, tolerant of the industry's
    naming zoo ("run line -1.5", "extra line", "spread - line", "handicap
    +1.5", "total runs u/o 5.5"). Segment/derived variants return None —
    exact-set membership left every differently-suffixed book off the board
    (lived: an NPB run-line board read 8 books NA while five books priced it)."""
    m = market.lower()
    if m in _H2H_MARKETS:
        return "h2h"
    if any(w in m for w in _SEGMENT_MARKERS):
        return None
    if (m.startswith("h2h") or "head to head" in m or "money line" in m
            or "moneyline" in m or "match winner" in m):
        # suffixed h2h labels ("h2h - win", "h2h - match (regular time)",
        # "h2h - head to head - including overtime") are the SAME market —
        # the settle-qualifier suffix is each book's house style
        return "h2h"
    if m in _TOTAL_MARKETS or "total" in m or "over/under" in m or "u/o" in m:
        return "total" if _only_market_words(m) else None
    if m in _LINE_MARKETS or "spread" in m or "handicap" in m or "line" in m:
        return "line" if _only_market_words(m) else None
    return None


# every word a FULL-GAME total/line label may carry — a token outside this
# vocabulary is a name ("fremantle - total points o/u" is a TEAM total wearing
# no "team" marker) and names make it a different market. Numeric tokens
# ("run line -1.5") pass freely.
_MARKET_WORDS = frozenset({
    "total", "totals", "points", "point", "goals", "goal", "runs", "run",
    "games", "game", "maps", "rounds", "match", "over", "under", "o/u", "u/o",
    "ou", "over/under", "line", "lines", "spread", "spreads", "handicap",
    "alternative", "alternate", "extra", "main", "-", "\u2013", "the",
})


def _only_market_words(market: str) -> bool:
    for token in market.replace("(", " ").replace(")", " ").split():
        if any(ch.isdigit() for ch in token):
            continue
        if token not in _MARKET_WORDS:
            return False
    return True


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
        # family-tolerant market keys: exact-set membership left every
        # suffixed label ("h2h - win", "total goals over/under", "run line")
        # unseedable — the engine only ever calibrated from Pinnacle/FanDuel
        # shaped boards. Soccer's DRAW leg rides book_quotes; the engine
        # anchors on the home/away pair and derives the draw itself.
        family = _market_family(row.market)
        side, line = _split_selection(row.selection.lower())
        odds = float(row.odds)
        if family == "h2h" and side in ("home", "away", "draw"):
            h2h[side] = odds
            book_quotes.append({"market": "h2h", "selection": side, "line": None, "odds": odds})
        elif family == "total" and side in ("over", "under") and line is not None:
            totals.setdefault(line, {})[side] = odds
            book_quotes.append({"market": "total", "selection": side, "line": line, "odds": odds})
        elif family == "line" and side in ("home", "away") and line is not None:
            book_quotes.append({"market": "line", "selection": side, "line": line, "odds": odds})
    paired = {ln: p for ln, p in totals.items() if len(p) == 2}
    if "home" not in h2h or "away" not in h2h or not paired:
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


def _quote_price_rows(
    rows: list[Price], sport: str, places: int | None
) -> dict[tuple[str, str, float | None], Price]:
    """The warehouse Price row behind each normalized derivative quote — the
    cross-book board and fixture lookups need the row a candidate came from.
    Keys mirror the input builders: (engine market, selection, line)."""
    lookup: dict[tuple[str, str, float | None], Price] = {}
    for row in rows:
        market = row.market.lower()
        if sport == "racing":
            if market == "place" and places is not None:
                lookup[("place", row.selection.lower(), float(places))] = row
            continue
        side, line = _split_selection(row.selection.lower())
        family = _market_family(row.market)
        if family == "h2h" and side in ("home", "away"):
            lookup[("h2h", side, None)] = row
        elif family == "total" and side in ("over", "under") and line is not None:
            lookup[("total", side, line)] = row
        elif family == "line" and side in ("home", "away") and line is not None:
            lookup[("line", side, line)] = row
    return lookup


async def _watch_model_value(
    session: AsyncSession, sub: Subscription, pusher: Pusher, *, now: dt.datetime
) -> int:
    """Engine fair prices vs the books' derivative quotes (consistency edge).

    One MESSAGE per market story: every book's candidates on the same
    fixture+market pool into a single alert carrying the engine fair, each
    flagged book's price, and the industry board with the highest price
    bolded — the market moved, here is the whole industry on it.

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
    anchor_families = ("h2h", "total")  # via _market_family — suffixed labels count

    # PASS 1: scan each (book, event) board — the engine calibrates to each
    # book's OWN anchors — and pool the candidates by the fixture-shared
    # (event, market) identity: one market story across the whole industry
    stories: dict[tuple[str, str], dict[str, Any]] = {}
    for (row_book, event_id), event_rows in sorted(by_event.items()):
        # anchor gate: scan only where the calibration inputs moved recently —
        # the derivatives themselves may be arbitrarily old change-points
        # (unchanged quote = current quote), which is exactly what we compare
        cutoff = now - max_age
        if sport == "racing":
            fresh_anchor = any(
                (r.changed_at if r.changed_at.tzinfo else r.changed_at.replace(tzinfo=dt.UTC)) > cutoff
                for r in event_rows if r.market.lower() == "win")
        else:
            fresh_anchor = any(
                (r.changed_at if r.changed_at.tzinfo else r.changed_at.replace(tzinfo=dt.UTC)) > cutoff
                for r in event_rows if _market_family(r.market) in anchor_families)
        if not fresh_anchor:
            continue
        snap = (await session.execute(
            select(OddsSnapshot)
            .where(OddsSnapshot.event_external_id == event_id,
                   OddsSnapshot.start_time.isnot(None))
            .order_by(OddsSnapshot.captured_at.desc()).limit(1)
        )).scalars().first()
        start_time = snap.start_time if snap else None
        if bool(sub.params.get("pre_match_only", True)) and _started(start_time):
            continue  # a started game's laggy derivatives are game state, not edge
        competition = str(((snap.meta if snap else None) or {}).get("competition") or "")
        if not competition:
            # the alerted book stamps no league (TAB never does) — ask the
            # fixture's SIBLINGS: Pinnacle stamps its competition on every
            # row, and without this an NPB game alerted through an MLB-only
            # coverage (lived: "ORIX v Fukuoka Softbank", 2026-07-08)
            competition = await _sibling_competition(
                session, event_rows[0].provider, event_id)
        from sportsdata_agents.operations.ingestion.coverage import (
            competition_covered,
            sport_restricted,
        )

        if competition:
            if not competition_covered(price_sport, competition):
                continue  # a league outside the operator's coverage (Pinnacle
                # prices NPB/KBO; the preferences say MLB) — no alerts for it
        elif sport_restricted(price_sport):
            continue  # restricted sport, league unknown even via siblings —
            # every covered league has a stamping book on its fixture, so
            # an unvouched game is NPB/KBO/summer-league, not MLB/NBA/AFL
        places = sub.params.get("places")
        if sport == "racing":
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
        if not scan["candidates"]:
            continue
        rows_by_quote = _quote_price_rows(event_rows, sport, int(places) if places else None)
        display = await _event_display_name(session, event_id) or event_id
        from sportsdata_agents.operations.resolution.resolver import split_sides

        if (sport != "racing" and display != event_id
                and split_sides(display) is None):
            continue  # a pseudo-event (a book lists player props / odd-even
            # novelties as their own "events") — its outcomes can pair up
            # like anchors and calibrate the engine to nonsense (lived:
            # "Chicago Cubs Runs Odd/Even" priced as a baseball fixture)
        if sport == "racing":
            event_key = _racing_board_key(display).lower()
        else:
            fixture = (await session.execute(
                select(Event.fixture_id).where(
                    Event.provider == event_rows[0].provider,
                    Event.external_id == event_id)
            )).scalar_one_or_none()
            event_key = str(fixture) if fixture else f"{event_rows[0].provider}:{event_id}"
        for candidate in scan["candidates"]:
            story = stories.setdefault((event_key, candidate["market"]), {
                "display": display, "start_time": start_time, "cands": []})
            story["cands"].append({
                **candidate, "book": row_book, "event_id": event_id,
                "row": rows_by_quote.get((candidate["market"],
                                          str(candidate["selection"]).lower(),
                                          candidate["line"]))})

    # PASS 2: one MESSAGE per market story — every flagged selection, every
    # book with edge, and the industry board with the highest price bolded
    fired = 0
    bankroll = float(sub.params.get("bankroll", 100.0))
    sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
    ordered = sorted(stories.items(),
                     key=lambda kv: -max(c["edge_pct"] for c in kv[1]["cands"]))
    for (event_key, market), story in ordered:
        if fired >= cap:
            break
        cands = sorted(story["cands"], key=lambda c: -c["edge_pct"])
        # LIVENESS: "unchanged change-point = current quote" is false for
        # alt-line ladders — a rung the book delisted when its line moved
        # keeps its last change-point forever (lived: Unibet 'away 1.5'
        # alerted 4 hours after the line walked to 4.5). A candidate whose
        # exact selection wasn't captured recently is not an offer.
        live_cands = []
        max_edge = float(sub.params.get("max_edge_pct", 30.0))
        for c in cands:
            if c["edge_pct"] > max_edge:
                continue  # an enormous "edge" is a data artifact — a flipped
                # side or a ghost rung, never a bet (lived: four +70% MLB run
                # lines pushed in one burst, all Sportsbet side inversions)
            c_row = c.get("row")
            if c_row is not None and not await _quote_still_listed(session, c_row, now):
                continue
            live_cands.append(c)
        cands = live_cands
        if not cands:
            continue
        # fold each selection's books into ONE line — the story reads
        # band suppression FIRST — cheaper than the board/nearest-line work
        # this loop otherwise does just to discard a same-band steady state
        band = int(cands[0]["edge_pct"] / 2.0)
        base_key = f"model_value:{event_key}:{market}"
        previously = (
            await session.execute(
                select(Alert)
                .where(Alert.subscription_id == sub.id,
                       Alert.dedupe_key.startswith(f"{base_key}:", autoescape=True))
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if previously is not None:
            prior_band = int(max(float(previously.payload.get("edge_pct", 0.0)),
                                 0.0) / 2.0)
            if band <= prior_band or not _cooled_down(previously.created_at):
                continue  # smaller/same band is the edge shrinking; a bigger
                # one re-fires only once the last alert is 20 minutes old
        # selection by selection, every book with edge on each
        by_sel: dict[tuple[str, float | None], list[dict[str, Any]]] = {}
        for c in cands:
            by_sel.setdefault((str(c["selection"]), c["line"]), []).append(c)
        lines = []
        for (selection, line), sel_cands in list(by_sel.items())[:4]:
            best = sel_cands[0]
            at_line = f" {line:g}" if line is not None else ""
            # one price per BOOK: a book quoting the same line through two
            # products (Dabble's over/under + pick-your-own-line) printed twice
            seen_books: set[str] = set()
            book_bits: list[str] = []
            for c in sel_cands:
                if c["book"] in seen_books:
                    continue
                seen_books.add(c["book"])
                book_bits.append(f"{c['book']} {c['odds']:.2f} (+{c['edge_pct']:.1f}%)")
                if len(book_bits) == 3:
                    break
            books_bit = " · ".join(book_bits)
            fair = float(best["model_fair_odds"] or 0.0)
            kelly = _kelly_stake(1.0 / fair if fair > 1.0 else 0.0,
                                 float(best["odds"]), bankroll)
            lines.append(
                f"**{selection}{at_line}**: {books_bit} · engine fair "
                f"{best['model_fair_odds']} · stake {_fmt_money(kelly)}")
        if len(by_sel) > 4:
            lines.append(f"…and {len(by_sel) - 4} more selections with edge")
        top = cands[0]
        quotes: dict[str, float] = {}
        thin: set[str] = set()
        if top.get("row") is not None:
            quotes = await _cross_book_quotes(session, top["row"])
            if not quotes and sport == "racing":
                quotes, thin = await _racing_board(
                    session, _racing_board_key(story["display"]),
                    top["row"].market, top["row"].selection, str(top["book"]),
                    start=story.get("start_time"))
        for c in cands:
            quotes.setdefault(str(c["book"]), float(c["odds"]))  # flagged books are prices, not NA
        include = await _coverage_pack(session, price_sport)
        jump = ""
        start_time = story["start_time"]
        if start_time is not None and not _started(start_time):
            jump = f" · Starts {_local_hhmm(start_time.isoformat(), _tz_for(sub))}"
        top_fair = float(top["model_fair_odds"]) if top["model_fair_odds"] else None
        top_line = f" {top['line']:g}" if top["line"] is not None else ""
        # alt-line honesty with CONTEXT: when no other book quotes this exact
        # line, show the industry's nearest quoted line instead of nothing
        near_note = ""
        flagged_books = {str(c["book"]) for c in cands}
        if (top["line"] is not None and top.get("row") is not None
                and not (set(quotes) - flagged_books)):
            near = await _nearest_line_quotes(session, top["row"], float(top["line"]))
            if near is not None:
                nl, nq = near
                cells = " · ".join(f"{b} {o:.2f}" for b, o in
                                   sorted(nq.items(), key=lambda kv: -kv[1])[:6])
                near_note = (f"\nnearest quoted line {nl:g} "
                             f"(no book has {top['line']:g}): {cells}")
        message = (
            f":crystal_ball: Model Value — {_sport_label(sport)} — {story['display']}\n"
            f"Market: {market} · Edge +{top['edge_pct']:.1f} percent{jump}\n"
            + "\n".join(lines)
            + f"\n{_fmt_money(bankroll)} bankroll"
            + _format_board(quotes, sharps, include, thin=thin, engine_fair=top_fair,
                            subject=f"{top['selection']}{top_line} · {market}")
            + near_note
        )
        # dedupe per market story, banded so a materially BIGGER edge
        # re-fires (the suppression check ran before the board work above)
        key = f"{base_key}:{band}"
        payload = {"market": market, "sport": sport, "event_key": event_key,
                   "book": top["book"], "event_external_id": top["event_id"],
                   "edge_pct": top["edge_pct"],
                   "candidates": [{k: v for k, v in c.items() if k != "row"}
                                  for c in cands[:12]]}
        if await _fire(session, sub, kind="model_value", key=key,
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
            if fired >= cap:
                break
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
        # NAME DESC puts the ANCHORED fair first — "engine:afl" > "engine-ratings:afl"
        # (ascending silently preferred the ratings fair: a stats model's number
        # wearing the "engine" label on every alert)
        .order_by(ModelArtifact.name.desc(), Prediction.predicted_at.desc())
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
        require_matched=bool(sub.params.get("require_matched", True)),
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
        matched_note = ""
        if candidate.get("exchange_matched"):
            matched_note = f" · Money matched {_fmt_money(candidate['exchange_matched'])}"
        title = "Exchange Value" if bool(sub.params.get("require_matched", True)) else "Sharp Value"
        message = (
            f":scales: {title} — {_sport_label(candidate['sport'])} — "
            f"{candidate['fixture']}\n"
            f"{candidate['book']} · Selection: **{candidate['outcome']}** · "
            f"Market: {candidate['market']}\n"
            f"Book price {candidate['odds']:.2f} against the {exchange_book} fair "
            f"{candidate['exchange_fair_odds']:.2f}{engine_note} · "
            f"Edge +{candidate['edge_pct']:.1f} percent{matched_note}\n"
            f"Suggested stake ${kelly:.2f} of {_fmt_money(bankroll)} bankroll"
            f"{_age_label(candidate.get('seen'), now or dt.datetime.now(dt.UTC))}\n"
            f"_Fair is {exchange_book} de-vigged; verify the leg is live_"
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
    """The human event name for an alert — snapshots carry it, ids do not.
    Accepts a fixture UUID too (value stories key by fixture since one game
    is one story) — the fixture's own name answers directly."""
    from sportsdata_agents.data.models import Fixture, OddsSnapshot

    name = (await session.execute(
        select(OddsSnapshot.event_name)
        .where(OddsSnapshot.event_external_id == event_id, OddsSnapshot.event_name != "")
        .order_by(OddsSnapshot.captured_at.desc()).limit(1)
    )).scalar_one_or_none()
    if name is not None:
        return name
    try:
        fixture_uuid = uuid.UUID(event_id)
    except ValueError:
        return None
    return (await session.execute(
        select(Fixture.name).where(Fixture.id == fixture_uuid)
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
                recent = " then ".join(f"{r['position']} of {r['field_size']}"
                                       for r in runs[:5])
                parts.append(f"last starts {recent}")
                parts.append(f"latest {runs[0]['age_days']:.0f} days ago")
            if runner.get("days_since_run") is not None and not runs:
                parts.append(f"{runner['days_since_run']} days since last run")
            note = "\nForm: " + " · ".join(parts) if parts else ""
            comment = str(runner.get("comment") or "").strip()
            if comment:
                note += f"\n_{comment[:220]}_"
            if note:
                return note
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
        min_matched=float(sub.params.get("min_matched", 1000.0)),
        max_lead_minutes=float(sub.params.get("max_lead_minutes", 60.0)),
        exclude_books=tuple(sub.params.get("exclude_books", ["FanDuel"])),
        min_consensus_books=int(sub.params.get("min_consensus_books", 3)),
        limit=cap * 3, now=now)
    bankroll = float(sub.params.get("bankroll", 100.0))
    # one MESSAGE per race: three runners with value in one race is one story
    by_race: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        # canonical race identity — the flagged book's own label ("MANAWATU
        # R1" vs "Manawatu R1") split one race into two alerts
        by_race.setdefault(_racing_board_key(str(candidate["race"])).lower(), []).append(candidate)
    fired = 0
    for race, cands in by_race.items():
        if fired >= cap:
            break
        keep: list[dict[str, Any]] = []
        for candidate in sorted(cands, key=lambda c: -c["edge_pct"]):
            engine_fair = await _engine_fair_for(
                session, "win", candidate.get("runner_number"),
                event_id=candidate.get("event_external_id"))
            exchange_backed = (candidate.get("versus")
                               == str(sub.params.get("exchange_book", "Betfair")))
            engine_backed = engine_fair is not None and engine_fair < candidate["odds"]
            # the racing signal is a SHARP opinion under the book's price —
            # Betfair's de-vigged fair or the engine's own. A consensus-only
            # edge (the pack median, nobody sharp shorter) stays silent;
            # require_sharp_fair=false restores the old behaviour
            if (bool(sub.params.get("require_sharp_fair", True))
                    and not exchange_backed and not engine_backed):
                continue
            if (sub.params.get("engine_gate") and engine_fair is not None
                    and engine_fair >= candidate["odds"] and not exchange_backed):
                continue  # engine says no value and no exchange corroboration
            kelly = _kelly_stake(1.0 / candidate["fair_odds"], candidate["odds"], bankroll)
            keep.append({**candidate, "engine_fair": engine_fair,
                         "kelly_stake": round(kelly, 2)})
        if not keep:
            continue
        top = keep[0]
        lines = []
        for c in keep[:4]:
            number = f" (#{c['runner_number']})" if c.get("runner_number") else ""
            fair_note = (f" · engine {c['engine_fair']:.2f}"
                         if c["engine_fair"] is not None else "")
            lines.append(
                f"Runner{number} **{c['runner']}**: {c['book']} win at "
                f"{c['odds']:.2f} · market fair {c['fair_odds']:.2f} "
                f"(versus {c['versus']}){fair_note} · edge +{c['edge_pct']:.1f} "
                f"percent · stake {_fmt_money(c['kelly_stake'])}")
        if len(keep) > 4:
            lines.append(f"…and {len(keep) - 4} more runners with value")
        jump = ""
        if top.get("start_time"):
            jump = f" · jumps {_local_hhmm(top['start_time'], _tz_for(sub))}"
        traded = ""
        if top.get("exchange_matched") is not None:
            traded = f" · {_fmt_money(top['exchange_matched'])} matched"
        # the board joins by saddle number when the scan knows it, else by
        # the runner's NAME (the number bridge maps both ways)
        top_start = (dt.datetime.fromisoformat(top["start_time"])
                     if top.get("start_time") else None)
        board, thin = await _racing_board(
            session, race, "win",
            str(top.get("runner_number") or top.get("runner") or ""),
            str(top["book"]), start=top_start)
        board.setdefault(str(top["book"]), float(top["odds"]))  # the flagged
        # book belongs ON its own board — its price anchors the comparison
        sharps = [str(b) for b in sub.params.get("sharp_books", ["Pinnacle", "Betfair"])]
        message = (
            f":racehorse: Racing Value — {top['race']}\n"
            + "\n".join(lines)
            + f"\n{_fmt_money(bankroll)} bankroll{traded}{jump}"
            + await _runner_form_note(session, top.get("race_no"),
                                      top.get("start_time"),
                                      top.get("runner_number"))
            + f"{_age_label(top.get('seen'), now or dt.datetime.now(dt.UTC))}"
            + _format_board(board, sharps, thin=thin, engine_fair=top["engine_fair"],
                            subject=f"{top['runner']} · win")
            + "\n_Check the live price before betting_"
        )
        # a materially bigger opportunity (a whole 25-point band) re-alerts;
        # the same race otherwise fires once per window
        bucket = int(top["edge_pct"] / 25.0)
        key = f"racing_value:{race}:{bucket}"
        payload = {"race": race, "runners": keep[:8], "bankroll": bankroll}
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
    max_edge = float(sub.params.get("max_edge_pct", 50.0))
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
        picks: list[dict[str, Any]] = []
        for number, prob in sorted(prob_by_number.items(), key=lambda kv: -kv[1]):
            if not 0.0 < prob < 1.0:
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
            if edge_pct > max_edge:
                continue  # a form fair THIS far under the whole market is a
                # model artifact (thin field, stale runs), not a 3x edge
            kelly = _kelly_stake(prob, effective, bankroll)
            picks.append({"number": number, "name": name, "prob": prob,
                          "back": back, "effective": effective,
                          "edge_pct": edge_pct, "kelly": kelly,
                          "matched": matched, "snap": snap})
        if not picks:
            continue
        # ONE message per race — three runners with form value in one race is
        # one story, not three pings
        picks.sort(key=lambda c: -c["edge_pct"])
        top = picks[0]
        board_key = _racing_board_key(top["snap"].event_name or "")
        title_race = (board_key if len(str(race.venue_mnemonic)) <= 4
                      else f"{race.venue_mnemonic} Race {race.race_number}")
        quotes, thin = await _racing_board(session, board_key, "win",
                                           str(top["number"]), exchange_book,
                                           start=race.start_time)
        quotes.setdefault(exchange_book, top["back"])  # the headline price is a price
        engine_fair = await _engine_fair_for(session, "win", str(top["number"]),
                                             event_id=race.race_key)
        lines = []
        for c in picks[:4]:
            lines.append(
                f"Runner {c['number']} — **{c['name'].title()}**: back at "
                f"{c['back']:.2f} (worth {c['effective']:.2f} after "
                f"{commission:.0%} commission) · form fair {1.0 / c['prob']:.2f} · "
                f"edge +{c['edge_pct']:.1f} percent · stake {_fmt_money(c['kelly'])}")
        if len(picks) > 4:
            lines.append(f"…and {len(picks) - 4} more runners with form value")
        form_note = await _runner_form_note(
            session, race.race_number,
            race.start_time.isoformat() if race.start_time else None,
            top["number"])
        sharps = [str(b) for b in sub.params.get("sharp_books",
                                                 ["Pinnacle", "Betfair"])]
        jump = (_local_hhmm(race.start_time.isoformat(), _tz_for(sub))
                if race.start_time else "?")
        message = (
            f":crystal_ball: Exchange value from form — {title_race}\n"
            + "\n".join(lines)
            + f"\nMoney matched {_fmt_money(float(top['matched']))} · "
              f"{_fmt_money(bankroll)} bankroll · Race starts {jump}"
            + form_note
            + _format_board(quotes, sharps,
                            await _coverage_pack(session, str(top["snap"].sport)),
                            thin=thin, engine_fair=engine_fair,
                            subject=f"{top['name'].title()} · win")
            + "\n_Consider Betfair Starting Price if the current price slips_"
        )
        key = f"bsp_value:{race.race_key}:{int(top['edge_pct'] / 5)}"
        payload = {"race_key": race.race_key,
                   "runners": [{"number": c["number"], "runner": c["name"],
                                "back": c["back"],
                                "form_fair": round(1.0 / c["prob"], 2),
                                "edge_pct": round(c["edge_pct"], 2),
                                "kelly_stake": round(c["kelly"], 2)} for c in picks],
                   "matched": top["matched"],
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
        # ONCE per question+outcome: these gaps are structural (settlement
        # rule differences between the venues) and persist for days — banded
        # re-fires read as spam
        key = (f"prediction_value:{candidate['polymarket_event']}"
               f":{candidate['outcome']}:{candidate['back']}")
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
                f"{book} · **{player} · {stat} {side} {line}**\n"
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
            # heavy watches opt into a slower lane: every_minutes=10 runs the
            # scan only when the pass minute aligns — stat_value alone costs
            # ~100s of ladder fitting, which every-minute passes cannot afford
            cadence = int(sub.params.get("every_minutes") or 0)
            if cadence > 1 and now.minute % cadence:
                continue
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
                    fired = await _watch_value(session, sub, push, now=now)
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
