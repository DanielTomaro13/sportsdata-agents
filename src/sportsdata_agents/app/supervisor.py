"""The desktop supervisor: gateway + conductor loop in one process."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import signal
from typing import Any

from sportsdata_agents.data.db import make_engine, make_sessionmaker
from sportsdata_agents.operations.scheduler import pace_for, run_tick, seconds_to_nearest_start

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TICK_SECONDS = 60


async def _conductor_loop(stop: asyncio.Event, *, tick_seconds: int = TICK_SECONDS) -> None:
    """Run the scheduler tick every ``tick_seconds`` until stopped. Each tick is
    the same deterministic ``run_tick`` the cron driver calls — pacing, locks,
    failure handoff all included. A tick that raises is logged and the loop
    survives (the app must not die because one cycle hiccupped)."""
    from sportsdata_agents.config import get_settings

    while not stop.is_set():
        started = dt.datetime.now()
        try:
            engine = make_engine(get_settings().database_url)
            try:
                pace = pace_for(await seconds_to_nearest_start(make_sessionmaker(engine)))
            finally:
                await engine.dispose()
            # run_tick is sync (spawns subprocesses) — keep the loop responsive
            report = await asyncio.to_thread(
                run_tick, now=dt.datetime.now(), period_s=float(tick_seconds), pace=pace
            )
            if report.ran or report.failed:
                logger.info("tick: ran=%s failed=%s pace=%s",
                            report.ran, report.failed, report.pace)
        except Exception as e:  # one bad tick never sinks the daemon
            logger.warning("conductor tick failed: %s", e)
        elapsed = (dt.datetime.now() - started).total_seconds()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, tick_seconds - elapsed))


async def _serve_gateway(stop: asyncio.Event, *, host: str, port: int, demo_only: bool) -> None:
    """Run the uvicorn gateway until ``stop`` is set."""
    import uvicorn

    from sportsdata_agents.gateway.app import create_app

    config = uvicorn.Config(
        create_app(demo_only=demo_only), host=host, port=port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    # the supervisor owns process signals; uvicorn must not install its own
    server.install_signal_handlers = lambda: None
    serve_task = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await serve_task


async def run_app_async(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    demo_only: bool = False,
    with_conductor: bool = True,
    tick_seconds: int = TICK_SECONDS,
) -> None:
    """Start the gateway and (optionally) the conductor loop; run until a signal."""
    from sportsdata_agents.paths import data_dir, migrate_legacy_layout

    moved = migrate_legacy_layout()
    if moved:
        logger.info("migrated legacy data into %s: %s", data_dir(), ", ".join(moved))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks SIGTERM
            loop.add_signal_handler(sig, stop.set)

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(_serve_gateway(stop, host=host, port=port, demo_only=demo_only))
    ]
    if with_conductor and not demo_only:  # a public demo node never runs the user's jobs
        tasks.append(asyncio.create_task(_conductor_loop(stop, tick_seconds=tick_seconds)))

    logger.info("sportsdata app up on http://%s:%d (conductor=%s)",
                host, port, with_conductor and not demo_only)
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_app(**kwargs: Any) -> None:
    """Blocking entry point for the `agents app` CLI command."""
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - signal path
        asyncio.run(run_app_async(**kwargs))
