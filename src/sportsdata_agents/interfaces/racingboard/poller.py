"""
Async orchestrator: discover races, poll money flow, update the store, broadcast.

Two loops run concurrently:
  * discovery loop  — every `discovery_interval`, refresh the race list from the
    TAB spine and (re)build the Betfair market index for the tracked venues.
  * price loop      — every `price_interval`, snapshot the N nearest-to-jump races
    across all sources and push updates to connected WebSocket clients.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .betfair import BetfairClient
from .config import settings
from .corporate import CorporateSource
from .engine import SportsDataEngine
from .sources import (
    BetfairMatcher,
    betfair_enrich,
    discover_races,
    finalize_snapshot,
    tab_snapshot,
)
from .store import Store


class Poller:
    def __init__(self, store: Store, broadcast=None) -> None:
        self.store = store
        self.broadcast = broadcast  # async callable(dict) or None
        self.engine = SportsDataEngine()
        self.betfair = BetfairClient() if settings.enable_betfair else None
        self.matcher = BetfairMatcher(self.betfair) if self.betfair else None
        self.corporate = CorporateSource() if settings.enable_corporate else None
        self._active_keys: list[str] = []
        self._running = False

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    async def start(self) -> None:
        self._running = True
        await self._discover_once()  # prime before serving
        await asyncio.gather(self._discovery_loop(), self._price_loop())

    async def stop(self) -> None:
        self._running = False
        if self.betfair:
            await self.betfair.aclose()

    # ---- discovery ----

    async def _discovery_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.discovery_interval)
            try:
                await self._discover_once()
            except Exception as exc:  # keep the loop alive
                print(f"[discovery] error: {exc}")

    async def _discover_once(self) -> None:
        date = self._today()
        races = await discover_races(self.engine, date)
        for ref in races:
            self.store.upsert_ref(ref)

        # Track the nearest-to-jump races at full cadence.
        races.sort(key=lambda r: r.start_time)
        active = races[: settings.max_active_races]
        self._active_keys = [r.race_key for r in active]

        # Build / refresh Betfair market index and stamp market ids onto refs.
        if self.matcher and settings.enable_betfair:
            try:
                await self.matcher.refresh_for(active)
                for r in active:
                    mid = self.matcher.market_id_for(r)
                    if mid:
                        self.store.races[r.race_key].ref.betfair_market_id = mid
            except Exception as exc:
                print(f"[discovery] betfair index error: {exc}")

        # Refresh corporate-book indices (Sportsbet / Pointsbet) for the day.
        if self.corporate:
            await self.corporate.refresh_indices(self.engine, date)

        # Drop races that are well past the jump to keep memory bounded.
        keep = {r.race_key for r in races}
        self.store.prune(keep)
        if self.corporate:
            self.corporate.prune(keep)
        print(f"[discovery] {len(races)} races tracked, {len(active)} active @ {time.strftime('%H:%M:%S')}")

    # ---- prices ----

    async def _price_loop(self) -> None:
        while self._running:
            try:
                await self._poll_active()
            except Exception as exc:
                print(f"[price] error: {exc}")
            await asyncio.sleep(settings.price_interval)

    async def _poll_active(self) -> None:
        keys = list(self._active_keys)
        # Snapshot each active race concurrently (bounded by upstream rate limits
        # inside the engine / Betfair client).
        await asyncio.gather(*(self._poll_race(k) for k in keys))
        if self.broadcast:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers()})

    async def _poll_race(self, race_key: str) -> None:
        st = self.store.races.get(race_key)
        if st is None:
            return
        ref = st.ref

        snap = None
        if settings.enable_tab:
            snap = await tab_snapshot(self.engine, ref)
        if snap is None:
            return

        if self.betfair and ref.betfair_market_id:
            try:
                await betfair_enrich(self.betfair, ref.betfair_market_id, snap)
            except Exception:
                pass

        if self.corporate:
            try:
                await self.corporate.enrich(self.engine, ref, snap)
            except Exception:
                pass

        finalize_snapshot(snap)
        self.store.add_snapshot(race_key, snap)

        if self.broadcast:
            detail = self.store.race_detail(race_key)
            if detail:
                await self.broadcast({"type": "race", "race_key": race_key, "detail": detail})
