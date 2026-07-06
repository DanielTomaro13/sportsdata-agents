"""The ingestion worker (M2.1): per-feed schedules, isolated failures, no LLM.

A ``Feed`` names an MCP tool, the groups its subprocess needs, a normalizer, and an
interval. ``ingest_once`` runs every (due) feed through one MCP manager;
``run_loop`` keeps doing that on each feed's own cadence. One feed failing logs and
counts — it never takes the loop or its siblings down (triage joins at M3.x).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.operations.ingestion.fetchers import (
    fetch_betr_all,
    fetch_betr_races,
    fetch_dabble_all,
    fetch_entain_all,
    fetch_fanduel_pages,
    fetch_fanduel_races,
    fetch_kalshi_all,
    fetch_pinnacle_all,
    fetch_pinnacle_books,
    fetch_pointsbet_all,
    fetch_pointsbet_books,
    fetch_pointsbet_races,
    fetch_pointsbet_racing_futures,
    fetch_polymarket_all,
    fetch_sportsbet_all,
    fetch_sportsbet_books,
    fetch_sportsbet_races,
    fetch_sportsbet_racing_futures,
    fetch_tab_all,
    fetch_tab_books,
    fetch_tab_races,
    fetch_tab_racing_futures,
    fetch_unibet_all,
    fetch_unibet_books,
    fetch_unibet_races,
    fetch_unibet_racing_futures,
)
from sportsdata_agents.operations.ingestion.normalizers import (
    PricePoint,
    normalize_betr_all,
    normalize_betr_races,
    normalize_dabble_all,
    normalize_entain_all,
    normalize_fanduel_pages,
    normalize_fanduel_races,
    normalize_kalshi_all,
    normalize_pinnacle_league,
    normalize_pointsbet_events,
    normalize_pointsbet_races,
    normalize_polymarket_all,
    normalize_sportsbet_all,
    normalize_sportsbet_books,
    normalize_sportsbet_races,
    normalize_tab_all,
    normalize_tab_books,
    normalize_tab_races,
    normalize_unibet_all,
    normalize_unibet_books,
    normalize_unibet_races,
)
from sportsdata_agents.operations.ingestion.store import record_points

logger = logging.getLogger(__name__)

# AU book payloads are MB-scale firehoses; the MCP's 150KB default guards MODEL
# context windows, which ingestion doesn't have — the ingest subprocess runs with
# this cap instead (the CLI passes it as SPORTSDATA_MCP_MAX_BYTES).
INGEST_MAX_BYTES = 8_000_000


@dataclass(frozen=True)
class Feed:
    name: str
    tool: str  # the MCP tool to call (label only when `fetch` is set)
    mcp_groups: tuple[str, ...]  # groups the subprocess must enable
    normalizer: Callable[[Any], list[PricePoint]]  # raw payload in — each guards its own shape
    provider: str = ""  # the provider string this feed's points carry (feed_health matches EXACTLY)
    arguments: dict[str, Any] | None = None
    interval_s: int = 300  # per-provider cadence
    # Multi-call providers (Pinnacle, PointsBet, Betfair): a fetcher composes the
    # discovery + price calls and returns the one payload the normalizer reads.
    fetch: Callable[[Any], Awaitable[Any]] | None = None


# The shipped feeds: ONE discovery-driven feed per provider — each walks the
# book's own discovery route every cycle, so coverage tracks whatever the book
# currently prices (all sports), not a hand-curated id list. Cadence and rotation
# caps reflect each book's payload economics (see fetchers.py).
FEEDS: dict[str, Feed] = {
    "sportsbet_all": Feed(
        name="sportsbet_all",
        provider="sportsbet",
        tool="sportsbet_nav_hierarchy",  # label; discovery walks nav -> competitions
        mcp_groups=("sportsbet.sports",),
        normalizer=normalize_sportsbet_all,
        fetch=fetch_sportsbet_all,
        interval_s=600,
    ),
    "tab_all": Feed(
        name="tab_all",
        provider="tab",
        tool="tab_sports",  # label; sports tree -> rotating competition pages
        mcp_groups=("tab.sports",),
        normalizer=normalize_tab_all,
        fetch=fetch_tab_all,
        interval_s=900,  # competition pages are MB-scale
    ),
    "unibet_all": Feed(
        name="unibet_all",
        provider="unibet",
        tool="unibet_kambi_call",  # label; group.json -> one listView per sport
        mcp_groups=("unibet.sport",),
        normalizer=normalize_unibet_all,
        fetch=fetch_unibet_all,
        interval_s=300,
    ),
    "entain_all": Feed(
        name="entain_all",
        provider="entain",
        tool="entain_sport_event_request",  # label; discovered categories -> bulk calls
        mcp_groups=("entain.rest", "entain.graphql"),  # graphql: SportingCategories discovery
        normalizer=normalize_entain_all,
        fetch=fetch_entain_all,
        interval_s=300,
    ),
    "pinnacle_all": Feed(
        name="pinnacle_all",
        provider="pinnacle",
        tool="pinnacle_sport_matchups_all",  # label; all sports, soonest matchups detailed
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="?"),  # _sport rides each matchup
        fetch=fetch_pinnacle_all,
        interval_s=300,
    ),
    "pointsbet_all": Feed(
        name="pointsbet_all",
        provider="pointsbet",
        tool="pointsbet_sports_list",  # label; full catalogue -> competition listings
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="?"),  # className labels each event
        fetch=fetch_pointsbet_all,
        interval_s=900,  # listings only — pointsbet_books owns the ~5MB details (B6)
    ),
    "betr_all": Feed(
        name="betr_all",
        provider="betr",
        tool="betr_master_category",  # label; one category call per event type
        mcp_groups=("betr.sport",),
        normalizer=normalize_betr_all,
        fetch=fetch_betr_all,
        interval_s=600,
    ),
    "dabble_all": Feed(
        name="dabble_all",
        provider="dabble",
        tool="dabble_active_competitions",  # label; discovery -> fixtures -> details
        mcp_groups=("dabble.sport",),
        normalizer=normalize_dabble_all,
        fetch=fetch_dabble_all,
        interval_s=600,
    ),
    "fanduel_us": Feed(
        name="fanduel_us",
        provider="fanduel",
        tool="fanduel_sb_call",  # label; sport pages -> event pages
        mcp_groups=("fanduel.sportsbook",),
        normalizer=partial(normalize_fanduel_pages, sport="?"),  # page id labels each page
        fetch=fetch_fanduel_pages,  # pages discovered from the nav scaffolding (B8)
        interval_s=900,
    ),
    "fanduel_racing_win": Feed(
        name="fanduel_racing_win",
        provider="fanduel_racing",
        tool="fanduel_racing_call",  # label; featured races -> race cards
        mcp_groups=("fanduel.racing",),
        normalizer=normalize_fanduel_races,
        fetch=fetch_fanduel_races,
        interval_s=120,  # racing prices move fast near post
    ),
    # ── full-book tier (60min): EVERY market of every fixture ──────────────
    # Entain/BetR/FanDuel/racing already deliver their full books through the hot
    # tier above; these five need (or deserve) a second pass: per-fixture firehoses
    # for Sportsbet/TAB/Unibet, full-board rotation for Pinnacle/PointsBet.
    "sportsbet_books": Feed(
        name="sportsbet_books",
        provider="sportsbet",
        tool="sportsbet_event_markets",
        mcp_groups=("sportsbet.sports",),
        normalizer=normalize_sportsbet_books,
        fetch=fetch_sportsbet_books,
        interval_s=3600,
    ),
    "tab_books": Feed(
        name="tab_books",
        provider="tab",
        tool="tab_match",
        mcp_groups=("tab.sports",),
        normalizer=normalize_tab_books,
        fetch=fetch_tab_books,
        interval_s=3600,
    ),
    "unibet_books": Feed(
        name="unibet_books",
        provider="unibet",
        tool="unibet_kambi_call",
        mcp_groups=("unibet.sport",),
        normalizer=normalize_unibet_books,
        fetch=fetch_unibet_books,
        interval_s=3600,
    ),
    "pinnacle_books": Feed(
        name="pinnacle_books",
        provider="pinnacle",
        tool="pinnacle_matchup_markets",
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="?"),
        fetch=fetch_pinnacle_books,
        interval_s=3600,
    ),
    "pointsbet_books": Feed(
        name="pointsbet_books",
        provider="pointsbet",
        tool="pointsbet_event",
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="?"),
        fetch=fetch_pointsbet_books,
        interval_s=3600,
    ),
    # ── racing tier: next-to-jump cards across every book that races ───────
    "tab_racing": Feed(
        name="tab_racing",
        provider="tab_racing",
        tool="tab_racing_race",
        mcp_groups=("tab.racing",),
        normalizer=normalize_tab_races,
        fetch=fetch_tab_races,
        interval_s=180,
    ),
    "sportsbet_racing": Feed(
        name="sportsbet_racing",
        provider="sportsbet_racing",
        tool="sportsbet_multiple_racecards",
        mcp_groups=("sportsbet.racing",),
        normalizer=normalize_sportsbet_races,
        fetch=fetch_sportsbet_races,
        interval_s=180,
    ),
    "betr_racing": Feed(
        name="betr_racing",
        provider="betr_racing",
        tool="betr_race",
        mcp_groups=("betr.racing",),
        normalizer=normalize_betr_races,
        fetch=fetch_betr_races,
        interval_s=180,
    ),
    "pointsbet_racing": Feed(
        name="pointsbet_racing",
        provider="pointsbet_racing",
        tool="pointsbet_racing_race",
        mcp_groups=("pointsbet.racing",),
        normalizer=normalize_pointsbet_races,
        fetch=fetch_pointsbet_races,
        interval_s=180,
    ),
    "unibet_racing": Feed(
        name="unibet_racing",
        provider="unibet_racing",
        tool="unibet_racing_call",
        mcp_groups=("unibet.racing",),
        normalizer=normalize_unibet_races,
        fetch=fetch_unibet_races,
        interval_s=300,
    ),
    # ── racing FUTURES tier (B11): ante-post Cup/carnival outrights ─────────
    # Priced months out and slow-moving — full-book cadence, rotating windows.
    "tab_racing_futures": Feed(
        name="tab_racing_futures",
        provider="tab_racing",
        tool="tab_racing_futures_race",
        mcp_groups=("tab.racing",),
        normalizer=normalize_tab_races,
        fetch=fetch_tab_racing_futures,
        interval_s=3600,
    ),
    "sportsbet_racing_futures": Feed(
        name="sportsbet_racing_futures",
        provider="sportsbet",
        tool="sportsbet_event_markets",
        mcp_groups=("sportsbet.racing", "sportsbet.sports"),
        normalizer=normalize_sportsbet_books,  # same {events:[{markets}]} packaging
        fetch=fetch_sportsbet_racing_futures,
        interval_s=3600,
    ),
    "pointsbet_racing_futures": Feed(
        name="pointsbet_racing_futures",
        provider="pointsbet",
        tool="pointsbet_racing_futures",
        mcp_groups=("pointsbet.racing", "pointsbet.sports"),
        normalizer=partial(normalize_pointsbet_events, sport="?"),
        fetch=fetch_pointsbet_racing_futures,
        interval_s=3600,
    ),
    "unibet_racing_futures": Feed(
        name="unibet_racing_futures",
        provider="unibet_racing",
        tool="unibet_racing_call",
        mcp_groups=("unibet.racing",),
        normalizer=normalize_unibet_races,
        fetch=fetch_unibet_racing_futures,
        interval_s=3600,
    ),
    # ── prediction markets tier (15min): probability venues ────────────────
    # Exchange quotes are probabilities captured as decimal odds (1/price) —
    # the warehouse, monitor and cross-book math read Kalshi/Polymarket like
    # any book. Slower cadence: these boards move on news, not on the clock.
    "kalshi_all": Feed(
        name="kalshi_all",
        provider="kalshi",
        tool="kalshi_events",  # label; open events with nested markets, cursor-paged
        mcp_groups=("kalshi.events",),
        normalizer=normalize_kalshi_all,
        fetch=fetch_kalshi_all,
        interval_s=900,
    ),
    "polymarket_all": Feed(
        name="polymarket_all",
        provider="polymarket",
        tool="polymarket_events",  # label; active Gamma events, volume-ordered pages
        mcp_groups=("polymarket.gamma",),
        normalizer=normalize_polymarket_all,
        fetch=fetch_polymarket_all,
        interval_s=900,
    ),
    # nba_cdn stays out (aggregator); Betfair stays out (no price sections via the
    # public readonly key from AU — fetcher+normalizer ready for an authed key, P4);
    # Entain RACING blocked on upstream persisted-query registration drift
    # (RacingRaceCardScreenWeb AND RacingFuturesScreen rejected even after
    # refresh-hashes — Entain sports feeds incl. their outrights are unaffected,
    # they're plain REST). X (Twitter) is a research surface, not a priced book —
    # its social.* capabilities serve agents directly, nothing to warehouse.
}


PACE_SCOPE_MAX_INTERVAL_S = 900  # only hot/prediction tiers accelerate


def paced_feeds(feeds: list[Feed], pace: int) -> list[Feed]:
    """Apply the proximity floor to the FAST tiers only. Flooring the 60-minute
    firehose tiers made one cycle outlast the racing cadence (observed live:
    racing feeds silent 40+ minutes behind a continuously-locked ingest)."""
    from dataclasses import replace

    return [
        replace(f, interval_s=min(f.interval_s, pace))
        if f.interval_s <= PACE_SCOPE_MAX_INTERVAL_S else f
        for f in feeds
    ]


def feeds_due_in_window(
    feeds: list[Feed], *, now_s: float, period_s: float
) -> list[Feed]:
    """The feeds whose interval boundary was crossed in the last ``period_s``
    seconds — STATELESS cron pacing: invoke `ingest --once --cron N` every N
    seconds and each feed runs at its own interval (a 180s racing feed every
    tick, the 3600s books tier only on the tick that crosses an hour boundary),
    with no daemon and no state file."""
    return [
        f for f in feeds
        if int(now_s // f.interval_s) != int((now_s - period_s) // f.interval_s)
    ]


async def ingest_once(
    manager: Any,  # MCPManager (Any: tests inject a fake with .call_tool)
    session_factory: async_sessionmaker[AsyncSession],
    feeds: list[Feed] | None = None,
) -> dict[str, Any]:
    """Run each feed once: fetch → normalize → record. Failures are per-feed."""
    feeds = feeds if feeds is not None else list(FEEDS.values())
    report: dict[str, Any] = {}
    for feed in feeds:
        try:
            if feed.fetch is not None:  # multi-call providers compose their own payload
                payload = await feed.fetch(manager)
            else:
                payload = await manager.call_tool(feed.tool, feed.arguments or {})
            points = feed.normalizer(payload)  # raw: shapes differ (TAB dict, sportsbet list)
            stats = await record_points(session_factory, points)
            report[feed.name] = {"ok": True, **stats}
            if not points:  # reachable but empty (off-season, shape drift) — visible, not silent
                report[feed.name]["note"] = "feed returned no price points"
            logger.info(
                "feed %s: %d points, %d price changes", feed.name, stats["snapshots"], stats["price_changes"]
            )
        except Exception as e:  # one bad feed must not sink the rest
            logger.warning("feed %s failed: %s: %s", feed.name, type(e).__name__, e)
            report[feed.name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return report


async def run_loop(
    manager: Any,
    session_factory: async_sessionmaker[AsyncSession],
    feeds: list[Feed] | None = None,
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    sleep: Callable[[float], Any] = asyncio.sleep,
    max_cycles: int | None = None,  # None = forever (CLI); tests bound it
) -> None:
    """Per-feed scheduling: each feed runs on its own interval, smallest gap first."""
    feeds = feeds if feeds is not None else list(FEEDS.values())
    next_due: dict[str, dt.datetime] = {f.name: now() for f in feeds}
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        current = now()
        due = [f for f in feeds if next_due[f.name] <= current]
        if due:
            await ingest_once(manager, session_factory, due)
            for f in due:
                next_due[f.name] = current + dt.timedelta(seconds=f.interval_s)
            cycles += 1
        wake = min(next_due.values())
        delay = max(0.0, (wake - now()).total_seconds())
        if delay:
            await sleep(min(delay, 30.0))  # cap so shutdown stays responsive
