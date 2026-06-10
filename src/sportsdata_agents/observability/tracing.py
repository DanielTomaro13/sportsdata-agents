"""Observability bootstrap (D8): Logfire when a token is configured, stdlib logging always.

The recorder's structured log lines (run_start / tool_call / run_end) ride whatever
logging backend is active — locally that's the console; with a Logfire token they are
captured and traced. Safe to call without a token (nothing is sent anywhere).
"""

from __future__ import annotations

import logging

from sportsdata_agents.config import Settings, get_settings

logger = logging.getLogger(__name__)


def setup_observability(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.logfire_token is None:
        return
    try:
        import logfire

        logfire.configure(token=settings.logfire_token.get_secret_value(), console=False)
        logger.info("logfire tracing enabled")
    except Exception as e:  # never let observability setup break the app
        logger.warning("logfire setup failed (%s: %s); continuing with stdlib logging", type(e).__name__, e)
