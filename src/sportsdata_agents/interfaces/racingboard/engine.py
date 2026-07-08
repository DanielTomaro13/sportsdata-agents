"""
Adapter that drives the sportsdata-mcp HTTP engine as a plain library.

We do NOT run the MCP protocol/server. We import the repo's spec loader + HTTP
client and build the same per-endpoint handlers the MCP would, then call them
directly. This gives fully-authed access (TAB's Akamai cookie handshake, rate
limits, retries, DoH where configured) for every book — the parts that a naive
standalone scraper gets wrong.

Usage:
    engine = SportsDataEngine()
    data = await engine.call("tab_racing_race", date="2026-07-08",
                             raceType="H", venueMnemonic="BAT", raceNumber=1)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .config import settings


class SportsDataEngine:
    def __init__(self, mcp_src: str | None = None) -> None:
        # Two ways to find the engine:
        #   1. installed package  — `pip install git+https://…/sportsdata-mcp`
        #      (used in Docker / cloud deploys); importable with no path.
        #   2. local src checkout — SPORTSDATA_MCP_SRC on PYTHONPATH (dev laptop).
        try:
            import sportsdata_mcp  # noqa: F401  (probe: already importable?)
        except ImportError:
            src = mcp_src or settings.sportsdata_mcp_src
            if not Path(src).exists():
                raise RuntimeError(
                    "sportsdata-mcp not found. Either `pip install "
                    "git+https://github.com/DanielTomaro13/sportsdata-mcp` or set "
                    f"SPORTSDATA_MCP_SRC to a checkout's src dir (tried {src!r})."
                )
            if src not in sys.path:
                sys.path.insert(0, src)

        from sportsdata_mcp.spec_loader import load_all_specs
        from sportsdata_mcp.config import load_config
        from sportsdata_mcp.http_client import HTTPClient
        from sportsdata_mcp.registry import make_endpoint_handler

        specs = load_all_specs()
        cfg = load_config()

        self._handlers: dict[str, Any] = {}
        self._clients = []  # keep HTTPClients alive (they own httpx sessions)
        for spec in specs:
            http = HTTPClient(spec.provider, cfg)
            self._clients.append(http)
            for ep in spec.endpoints:
                self._handlers[ep.name] = make_endpoint_handler(ep, http)

    def has(self, name: str) -> bool:
        return name in self._handlers

    async def call(self, name: str, **params: Any) -> Any:
        """Invoke an endpoint by its sportsdata-mcp tool name. Raises on HTTP error."""
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown sportsdata endpoint {name!r}")
        return await handler(**params)

    async def try_call(self, name: str, **params: Any) -> Any | None:
        """Like call() but swallows upstream errors, returning None.

        Racing markets 404 before they form and books rate-limit; the poller
        should keep going rather than crash on one bad race.
        """
        try:
            return await self.call(name, **params)
        except Exception:
            return None
