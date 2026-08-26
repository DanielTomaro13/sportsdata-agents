"""Capture live match state — the producer `match_state` was waiting for.

WHY THIS IS NOT A `Feed`. The price feeds walk a book's whole discovery route every
cycle and emit `PricePoint`s. This emits match state, on a different cadence, against a
different risk: in-play polling is the one thing on this platform that can get a USER
banned from a bookmaker, from their own IP, for reasons they never see. It deserves its
own path with its own bounds rather than a flag on the price machinery.

THE POLL BUDGET IS THE DESIGN, not a parameter on it. Three bounds, in order of how much
they save:

1. **Only fixtures that could plausibly be running.** A match is a candidate from its
   start time until a per-sport maximum duration after it. Everything else is not "cheap
   to poll", it is pointless to poll, and this is what stops the cost tracking the size
   of the catalogue instead of the number of live matches.
2. **A per-cycle cap.** A Saturday afternoon has more simultaneous matches than any
   sensible request rate covers. Truncating is correct: a watch that sees most live
   matches every 90 seconds is useful, and one that sees all of them and gets the user
   rate-limited is not.
3. **Default off.** Nothing polls until an operator turns it on.

Deliberately NOT here: retry, backoff and per-provider rate limiting inside a cycle. The
caller supplies the fetch, so those belong to whatever drives it — inventing a second
retry policy beside the one the MCP client already has is how two disagreeing backoffs
end up racing each other.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Fixture, MatchState

log = logging.getLogger(__name__)

#: How long after its start a fixture could still be running. Generous on purpose: the
#: cost of an over-long window is a few wasted polls, and the cost of a short one is
#: going blind during extra time — which is exactly when in-play prices matter most.
MAX_DURATION_MINUTES: dict[str, int] = {
    "afl": 180,
    "nrl": 150,
    "rugby_league": 150,
    "rugby_union": 150,
    "soccer": 180,        # 90 + stoppage + half-time, and extra time in a cup tie
    "basketball": 180,
    "nba": 180,
    "baseball": 300,      # no clock; extra innings are unbounded in principle
    "mlb": 300,
    "ice_hockey": 210,
    "nhl": 210,
    "tennis": 360,        # five sets, and the reason a single default would be wrong
    "cricket": 600,       # a T20; anything longer is not an in-play market worth polling
    "racing": 20,         # a race is minutes — the window is for scratchings and delays
}
DEFAULT_DURATION_MINUTES = 240

#: Matches polled per cycle. A Saturday has more simultaneous fixtures than a sane
#: request rate covers, and truncating beats being rate-limited.
DEFAULT_MAX_EVENTS = 40

#: A fixture is a candidate slightly BEFORE its listed start: listed times are estimates
#: and a match that begins early is exactly the one worth catching.
LEAD_MINUTES = 10


@dataclass(frozen=True)
class LiveState:
    """One observation of a match, as a provider reported it."""

    provider: str
    sport: str
    event_external_id: str
    status: str                     # pre | live | suspended | ended
    home_score: int | None = None
    away_score: int | None = None
    clock: str | None = None


def _window(sport: str) -> int:
    return MAX_DURATION_MINUTES.get(sport.lower(), DEFAULT_DURATION_MINUTES)


async def candidate_fixtures(
    session: AsyncSession, *, now: dt.datetime, max_events: int = DEFAULT_MAX_EVENTS
) -> list[Fixture]:
    """Fixtures that could be running right now, soonest-started first.

    This is the whole poll budget. Without it a capture pass costs whatever the catalogue
    happens to be; with it, it costs the number of matches actually on — which is the
    number the work is worth doing for.

    A fixture with no `start_time` is excluded rather than assumed live. Guessing wrong
    here means polling a book about a match that finished last week, repeatedly.
    """
    widest = max(*MAX_DURATION_MINUTES.values(), DEFAULT_DURATION_MINUTES)
    rows = list(
        (
            await session.execute(
                select(Fixture)
                .where(Fixture.start_time.is_not(None))
                .where(Fixture.start_time > now - dt.timedelta(minutes=widest))
                .where(Fixture.start_time < now + dt.timedelta(minutes=LEAD_MINUTES))
                .order_by(Fixture.start_time)
            )
        ).scalars()
    )
    live: list[Fixture] = []
    for fixture in rows:
        start = fixture.start_time
        if start is None:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.UTC)
        # The coarse SQL window uses the widest sport's duration; this narrows it to the
        # fixture's OWN sport, so a race stops being a candidate 20 minutes in rather
        # than sharing cricket's ten hours.
        if start + dt.timedelta(minutes=_window(fixture.sport)) < now:
            continue
        live.append(fixture)
        if len(live) >= max_events:
            log.info("in-play capture: %d candidates, capped at %d", len(rows), max_events)
            break
    return live


async def record_states(
    session: AsyncSession, states: list[LiveState], *, captured_at: dt.datetime
) -> int:
    """Persist observations as change-points: a row only when something MOVED.

    Same shape as `prices`, for the same reason — a score that has not changed is not
    news, and a row per poll would grow without bound while saying nothing. Returns the
    number of rows actually written.
    """
    written = 0
    for state in states:
        latest = (
            await session.execute(
                select(MatchState)
                .where(MatchState.provider == state.provider)
                .where(MatchState.event_external_id == state.event_external_id)
                .order_by(MatchState.captured_at.desc())
                .limit(1)
            )
        ).scalar()
        if latest is not None and (
            latest.status == state.status
            and latest.home_score == state.home_score
            and latest.away_score == state.away_score
            and latest.clock == state.clock
        ):
            continue  # unchanged: nothing to record
        session.add(
            MatchState(
                captured_at=captured_at,
                provider=state.provider,
                sport=state.sport,
                event_external_id=state.event_external_id,
                status=state.status,
                home_score=state.home_score,
                away_score=state.away_score,
                clock=state.clock,
            )
        )
        written += 1
    await session.flush()
    return written


async def capture_once(
    session: AsyncSession,
    fetch: Callable[[list[Fixture]], Awaitable[list[LiveState]]],
    *,
    now: dt.datetime | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> dict[str, Any]:
    """One capture pass: pick the candidates, ask for their state, record what moved.

    `fetch` is injected rather than built here. The provider call is the part that cannot
    be tested without hitting a bookmaker, so it stays at the edge — everything that
    decides WHAT to poll and WHAT to keep is a pure function of the warehouse and covered
    by tests.
    """
    now = now or dt.datetime.now(dt.UTC)
    candidates = await candidate_fixtures(session, now=now, max_events=max_events)
    if not candidates:
        return {"candidates": 0, "observed": 0, "recorded": 0}
    states = await fetch(candidates)
    recorded = await record_states(session, states, captured_at=now)
    return {"candidates": len(candidates), "observed": len(states), "recorded": recorded}
