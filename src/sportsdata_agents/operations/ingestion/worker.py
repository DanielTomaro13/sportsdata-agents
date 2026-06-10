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
    fetch_pinnacle_league,
    fetch_pointsbet_competition,
)
from sportsdata_agents.operations.ingestion.normalizers import (
    PricePoint,
    normalize_betr_category,
    normalize_entain_events,
    normalize_nba_odds,
    normalize_pinnacle_league,
    normalize_pointsbet_events,
    normalize_sportsbet_matches,
    normalize_tab_competition,
    normalize_unibet_matches,
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


# The shipped feeds. Adding a provider = one normalizer + one row here.
FEEDS: dict[str, Feed] = {
    "nba_odds": Feed(
        name="nba_odds",
        tool="nba_odds_today",
        mcp_groups=("nba.public.cdn",),  # the data plane's group names are dotted
        normalizer=normalize_nba_odds,
        interval_s=300,
    ),
    "sportsbet_afl_h2h": Feed(
        name="sportsbet_afl_h2h",
        tool="sportsbet_competition_matches",
        mcp_groups=("sportsbet.sports",),
        normalizer=partial(normalize_sportsbet_matches, sport="afl"),
        arguments={"competitionId": 4165},  # verified id from the book catalogue
        interval_s=300,
    ),
    "tab_afl_h2h": Feed(
        name="tab_afl_h2h",
        tool="tab_competition",
        mcp_groups=("tab.sports",),
        normalizer=partial(normalize_tab_competition, sport="afl"),
        arguments={"sport": "AFL Football", "competition": "AFL", "numTopMarkets": 1},
        interval_s=300,
    ),
    "sportsbet_nrl_h2h": Feed(
        name="sportsbet_nrl_h2h",
        tool="sportsbet_competition_matches",
        mcp_groups=("sportsbet.sports",),
        normalizer=partial(normalize_sportsbet_matches, sport="nrl"),
        arguments={"competitionId": 3436},
        interval_s=300,
    ),
    "tab_nrl_h2h": Feed(
        name="tab_nrl_h2h",
        tool="tab_competition",
        mcp_groups=("tab.sports",),
        normalizer=partial(normalize_tab_competition, sport="nrl"),
        arguments={"sport": "Rugby League", "competition": "NRL", "numTopMarkets": 1},
        interval_s=300,
    ),
    "unibet_afl_h2h": Feed(
        name="unibet_afl_h2h",
        tool="unibet_kambi_call",
        mcp_groups=("unibet.sport",),
        normalizer=partial(normalize_unibet_matches, sport="afl"),
        arguments={"operation": "sport_matches", "path_params": {"sport": "australian_rules"}},
        interval_s=300,
    ),
    "unibet_nrl_h2h": Feed(
        name="unibet_nrl_h2h",
        tool="unibet_kambi_call",
        mcp_groups=("unibet.sport",),
        normalizer=partial(normalize_unibet_matches, sport="nrl"),
        arguments={"operation": "sport_matches", "path_params": {"sport": "rugby_league"}},
        interval_s=300,
    ),
    "betr_afl_h2h": Feed(
        name="betr_afl_h2h",
        tool="betr_sports_category",
        mcp_groups=("betr.sport",),
        normalizer=partial(normalize_betr_category, sport="afl"),
        arguments={"CategoryId": 43735},  # AFL Premiership (discovered live via betr_master_category)
        interval_s=300,
    ),
    "entain_afl_h2h": Feed(
        name="entain_afl_h2h",
        tool="entain_sport_event_request",
        mcp_groups=("entain.rest",),
        normalizer=partial(normalize_entain_events, sport="afl"),
        arguments={"category_ids": ["23d497e6-8aab-4309-905b-9421f42c9bc5"]},  # Australian Rules
        interval_s=300,
    ),
    "entain_nrl_h2h": Feed(
        name="entain_nrl_h2h",
        tool="entain_sport_event_request",
        mcp_groups=("entain.rest",),
        normalizer=partial(normalize_entain_events, sport="nrl"),
        arguments={"category_ids": ["608a1803-45bc-465a-8471-c89dcb68a27d"]},  # Rugby League
        interval_s=300,
    ),
    "pinnacle_afl_h2h": Feed(
        name="pinnacle_afl_h2h",
        tool="pinnacle_league_matchups",  # label; the fetcher composes matchups + markets
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="afl"),
        fetch=partial(fetch_pinnacle_league, league_id=5448),
        interval_s=300,
    ),
    "pinnacle_nrl_h2h": Feed(
        name="pinnacle_nrl_h2h",
        tool="pinnacle_league_matchups",
        mcp_groups=("pinnacle.sports",),
        normalizer=partial(normalize_pinnacle_league, sport="nrl"),
        fetch=partial(fetch_pinnacle_league, league_id=1654),
        interval_s=300,
    ),
    "pointsbet_afl_h2h": Feed(
        name="pointsbet_afl_h2h",
        tool="pointsbet_competition_events",  # label; per-event details are ~5MB each
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="afl"),
        fetch=partial(fetch_pointsbet_competition, competition_key=7523),
        interval_s=900,  # heavy payloads — slower cadence on purpose
    ),
    "pointsbet_nrl_h2h": Feed(
        name="pointsbet_nrl_h2h",
        tool="pointsbet_competition_events",
        mcp_groups=("pointsbet.sports",),
        normalizer=partial(normalize_pointsbet_events, sport="nrl"),
        fetch=partial(fetch_pointsbet_competition, competition_key=7593),
        interval_s=900,
    ),
    # Betfair is NOT registered: the public readonly key serves market/runner data
    # but returns no RUNNER_EXCHANGE_PRICES_BEST sections from AU (delayed-data key;
    # verified live 2026-06-11 incl. on a $26K-matched market). The fetcher +
    # normalizer are ready — add the row back when an authenticated Exchange API
    # key lands (P4).
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
