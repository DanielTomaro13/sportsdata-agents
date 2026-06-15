"""The desktop supervisor: gateway + conductor loop in one process."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import signal
import threading
import time
from collections.abc import Awaitable, Callable
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


async def _supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    *,
    base_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> None:
    """Run ``factory()`` and RESTART it with exponential backoff if it exits or
    crashes before ``stop`` is set — so a transient gateway error (a dropped port,
    a bad upstream) doesn't take the whole desktop app down. A child that ran a
    long time resets the backoff; rapid crashes escalate it (capped)."""
    backoff = base_backoff
    while not stop.is_set():
        started = time.monotonic()
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the child crashed — log and retry, never propagate
            logger.warning("%s crashed: %s", name, e)
        if stop.is_set():
            return
        backoff = base_backoff if (time.monotonic() - started) > max_backoff else backoff
        logger.info("%s exited; restarting in %.1fs", name, backoff)
        with contextlib.suppress(TimeoutError):  # stop firing during backoff ends it cleanly
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return
        backoff = min(backoff * 2, max_backoff)


async def _serve_gateway(stop: asyncio.Event, *, host: str, port: int, demo_only: bool) -> None:
    """Run the uvicorn gateway until ``stop`` is set."""
    import uvicorn

    from sportsdata_agents.gateway.app import create_app

    config = uvicorn.Config(
        create_app(demo_only=demo_only), host=host, port=port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    # the supervisor owns process signals; uvicorn must not install its own
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
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
    install_signals: bool = True,
    external_stop: threading.Event | None = None,
) -> None:
    """Start the gateway and (optionally) the conductor loop; run until stopped.

    ``install_signals`` registers SIGINT/SIGTERM handlers (only valid on the main
    thread — the desktop-window mode runs this in a worker thread and passes False).
    ``external_stop`` is a ``threading.Event`` the caller (the native window) sets
    when it wants the daemon to shut down — bridged to the internal asyncio stop."""
    from sportsdata_agents.paths import data_dir, migrate_legacy_layout

    moved = migrate_legacy_layout()
    if moved:
        logger.info("migrated legacy data into %s: %s", data_dir(), ", ".join(moved))

    # Self-create the desktop SQLite warehouse schema on first launch (no-op when it
    # already exists or on the alembic-managed server path). Guarded: a DB hiccup
    # degrades to no-audit, it never blocks the app from starting.
    try:
        from sportsdata_agents.data.db import ensure_schema
        await ensure_schema()
    except Exception as e:
        logger.warning("could not ensure warehouse schema: %s", e)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows lacks SIGTERM
                loop.add_signal_handler(sig, stop.set)

    bridge_task: asyncio.Task[Any] | None = None
    if external_stop is not None:
        async def _bridge() -> None:
            while not external_stop.is_set():
                await asyncio.sleep(0.25)
            stop.set()
        bridge_task = asyncio.create_task(_bridge())

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(_supervise(
            "gateway", lambda: _serve_gateway(stop, host=host, port=port, demo_only=demo_only), stop
        ))
    ]
    if with_conductor and not demo_only:  # a public demo node never runs the user's jobs
        tasks.append(asyncio.create_task(_supervise(
            "conductor", lambda: _conductor_loop(stop, tick_seconds=tick_seconds), stop
        )))

    logger.info("sportsdata app up on http://%s:%d (conductor=%s)",
                host, port, with_conductor and not demo_only)
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        if bridge_task is not None:
            bridge_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *( [bridge_task] if bridge_task else [] ), return_exceptions=True)


def run_app(**kwargs: Any) -> None:
    """Blocking entry point for the `agents app` CLI command."""
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - signal path
        asyncio.run(run_app_async(**kwargs))
