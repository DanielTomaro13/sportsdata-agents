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
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.operations.ingestion.normalizers import (
    PricePoint,
    normalize_nba_odds,
    normalize_sportsbet_matches,
    normalize_tab_competition,
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
    tool: str  # the MCP tool to call
    mcp_groups: tuple[str, ...]  # groups the subprocess must enable
    normalizer: Callable[[Any], list[PricePoint]]  # raw payload in — each guards its own shape
    arguments: dict[str, Any] | None = None
    interval_s: int = 300  # per-provider cadence


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
