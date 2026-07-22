"""Self-contained live mode for the sports board.

Runs the ingestion poll loop *in-process* so the server fetches live prices into
its own store and serves them directly — no external ``agents ingest`` process and
(with an ephemeral SQLite url) no durable warehouse. This is the racing board's
in-process-poller model applied to sports: one process = a live board.

Opt-in via ``SPORTSBOARD_LIVE=1`` (see deploy/serve_live.sh, deploy/render.yaml).
Default off, so the plain server stays a pure warehouse reader — the static build
and the tests are unaffected. The money-flow window fills over the first few
minutes as snapshots accumulate, exactly like the racing board on a cold start.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("sportsboard.live")

_TRUTHY = {"1", "true", "yes", "on"}
_RESOLVE_EVERY_S = 60.0  # cross-provider fixture resolution cadence (DB-only, no MCP)


def live_enabled() -> bool:
    """Whether the in-process live poller should run (SPORTSBOARD_LIVE)."""
    return os.environ.get("SPORTSBOARD_LIVE", "").strip().lower() in _TRUTHY


async def _resolve_loop(sf: async_sessionmaker[AsyncSession]) -> None:
    """Re-link each provider's events into shared fixtures on a fixed cadence
    (DB-only — no MCP), so newly-ingested games join the board."""
    from sportsdata_agents.operations.resolution import resolve_events

    while True:
        await asyncio.sleep(_RESOLVE_EVERY_S)
        try:
            await resolve_events(sf)
        except Exception:
            logger.exception("resolve tick failed")


async def run_poller() -> None:
    """Prime once, then run the per-feed ingestion loop against the board's store.

    Reads the same ``database_url`` the reader serves from, so the loop and the API
    share one store. Any failure (no MCP, no network) is logged, not fatal — the
    board just serves an empty/thin slate until upstreams recover.
    """
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.db import make_engine, make_sessionmaker
    from sportsdata_agents.mcp.manager import MCPManager
    from sportsdata_agents.operations.ingestion import ingest_once, run_loop
    from sportsdata_agents.operations.ingestion.worker import INGEST_MAX_BYTES, tuned_feeds
    from sportsdata_agents.operations.resolution import resolve_events
    from sportsdata_agents.tools.ops import disabled_feeds

    settings = get_settings()
    feeds = [f for f in tuned_feeds() if f.name not in disabled_feeds()]
    # optional allow-list (SPORTSBOARD_LIVE_FEEDS="pinnacle,betfair") — run a lean
    # board, or scope a smoke test to one feed instead of the full book blast.
    only = {n.strip() for n in os.environ.get("SPORTSBOARD_LIVE_FEEDS", "").split(",") if n.strip()}
    if only:
        feeds = [f for f in feeds if f.name in only]
    if not feeds:
        logger.warning("live mode: no enabled feeds — nothing to poll")
        return
    groups = sorted({g for f in feeds for g in f.mcp_groups})

    engine = make_engine(settings.database_url)
    try:
        async with engine.begin() as conn:  # additive + idempotent (SQLite/ephemeral live store)
            await conn.run_sync(Base.metadata.create_all)
        sf = make_sessionmaker(engine)
        # ETL has no model context window to protect — lift the MCP byte cap (AU book
        # payloads are MB-scale; see INGEST_MAX_BYTES).
        async with MCPManager(
            groups=groups,
            command=settings.mcp_command,
            extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(INGEST_MAX_BYTES)},
        ) as manager:
            # prime: one capture, then link each provider's events into fixtures so
            # the board can blend the sharp line across books for a single game.
            logger.info("live mode: priming %d feeds …", len(feeds))
            await ingest_once(manager, sf, feeds)
            await resolve_events(sf)
            logger.info("live mode: entering poll + resolve loops")
            # ingest per feed cadence; resolve on its own timer (DB-only, no MCP).
            await asyncio.gather(run_loop(manager, sf, feeds), _resolve_loop(sf))
    except Exception:  # a live-poll failure must never take the server down
        logger.exception("live poller stopped")
    finally:
        await engine.dispose()
