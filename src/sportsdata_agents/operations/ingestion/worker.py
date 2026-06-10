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
    fetch_entain_all,
    fetch_fanduel_pages,
    fetch_fanduel_races,
    fetch_pinnacle_all,
    fetch_pinnacle_books,
    fetch_pointsbet_all,
    fetch_pointsbet_books,
    fetch_sportsbet_all,
    fetch_sportsbet_books,
    fetch_tab_all,
    fetch_tab_books,
    fetch_unibet_all,
    fetch_unibet_books,
)
from sportsdata_agents.operations.ingestion.normalizers import (
    PricePoint,
    normalize_betr_all,
    normalize_entain_all,
    normalize_fanduel_pages,
    normalize_fanduel_races,
    normalize_pinnacle_league,
    normalize_pointsbet_events,
    normalize_sportsbet_all,
    normalize_sportsbet_books,
    normalize_tab_all,
    normalize_tab_books,
    normalize_unibet_all,
    normalize_unibet_books,
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
        tool="sportsbet_nav_hierarchy",  # label; discovery walks nav -> competitions
        mcp_groups=("sportsbet.sports",),
        normalizer=normalize_sportsbet_all,
        fetch=fetch_sportsbet_all,
        interval_s=600,
    ),
    "tab_all": Feed(
        name="tab_all",
        tool="tab_sports",  # label; sports tree -> rotating competition pages
        mcp_groups=("tab.sports",),
        normalizer=normalize_tab_all,
        fetch=fetch_tab_all,
        interval_s=900,  # competition pages are MB-scale
    ),
    "unibet_all": Feed(
        name="unibet_all",
        tool="unibet_kambi_call",  # label; group.json -> one listView per sport
        mcp_groups=("unibet.sport",),
        normalizer=normalize_unibet_all,
        fetch=fetch_unibet_all,
        interval_s=300,
    ),
    "entain_all": Feed(
        name="entain_all",
        tool="entain_sport_event_request",  # label; one bulk call per sport category
        mcp_groups=("entain.rest",),
        normalizer=normalize_entain_all,
        fetch=fetch_entain_all,
        interval_s=300,
    ),
    "pinnacle_all": Feed(
        name="pinnacle_all",
        tool="pinnacle_sport_matchups_all",  # label; all sports, soonest matchups detailed
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="?"),  # _sport rides each matchup
        fetch=fetch_pinnacle_all,
        interval_s=300,
    ),
    "pointsbet_all": Feed(
        name="pointsbet_all",
        tool="pointsbet_sports_list",  # label; full catalogue -> soonest event details
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="?"),  # className labels each event
        fetch=fetch_pointsbet_all,
        interval_s=1800,  # ~5MB per event detail
    ),
    "betr_all": Feed(
        name="betr_all",
        tool="betr_master_category",  # label; one category call per event type
        mcp_groups=("betr.sport",),
        normalizer=normalize_betr_all,
        fetch=fetch_betr_all,
        interval_s=600,
    ),
    "fanduel_us": Feed(
        name="fanduel_us",
        tool="fanduel_sb_call",  # label; sport pages -> event pages
        mcp_groups=("fanduel.sportsbook",),
        normalizer=partial(normalize_fanduel_pages, sport="?"),  # page id labels each page
        fetch=partial(fetch_fanduel_pages, page_ids=["nba", "mlb", "nhl", "wnba", "mls", "ufc"]),
        interval_s=900,
    ),
    "fanduel_racing_win": Feed(
        name="fanduel_racing_win",
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
        tool="sportsbet_event_markets",
        mcp_groups=("sportsbet.sports",),
        normalizer=normalize_sportsbet_books,
        fetch=fetch_sportsbet_books,
        interval_s=3600,
    ),
    "tab_books": Feed(
        name="tab_books",
        tool="tab_match",
        mcp_groups=("tab.sports",),
        normalizer=normalize_tab_books,
        fetch=fetch_tab_books,
        interval_s=3600,
    ),
    "unibet_books": Feed(
        name="unibet_books",
        tool="unibet_kambi_call",
        mcp_groups=("unibet.sport",),
        normalizer=normalize_unibet_books,
        fetch=fetch_unibet_books,
        interval_s=3600,
    ),
    "pinnacle_books": Feed(
        name="pinnacle_books",
        tool="pinnacle_matchup_markets",
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="?"),
        fetch=fetch_pinnacle_books,
        interval_s=3600,
    ),
    "pointsbet_books": Feed(
        name="pointsbet_books",
        tool="pointsbet_event",
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="?"),
        fetch=fetch_pointsbet_books,
        interval_s=3600,
    ),
    # nba_cdn stays out (aggregator); Betfair stays out (no price sections via the
    # public readonly key from AU — fetcher+normalizer ready for an authed key, P4).
}


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
