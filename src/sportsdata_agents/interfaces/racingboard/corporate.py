"""
Corporate fixed-odds books (Sportsbet, Pointsbet) as an extra price signal.

For each active race we resolve the book's own race id (venue + race number +
code) and pull win prices per runner — giving a live odds comparison and a
best-price-on-the-market read alongside the tote pool share and Betfair WoM.

Only Sportsbet + Pointsbet are wired: both cleanly expose win prices with
movement. Ladbrokes/Neds (Entain) is intentionally omitted — its public racecard
route 404s without auth — and Dabble too (per-race fixture matching is too heavy
for fast polling). The book list is pluggable, so either can be added later.

Prices are fetched on a slower cadence than the tote (they rate-limit) and cached,
so every snapshot carries the latest corporate prices even between fetches.
"""

from __future__ import annotations

import time
from typing import Any

from .config import settings
from .engine import SportsDataEngine
from .models import RaceRef, RaceSnapshot
from .sources import _norm_runner, _norm_venue, _venue_compatible


# ---- code mapping per book (verified live) ----
PB_TYPE_TO_CODE = {1: "R", 2: "H", 3: "G", 4: "G"}


def _sb_code(class_name: str) -> str:
    n = (class_name or "").lower()
    if "greyhound" in n:
        return "G"
    if "harness" in n:
        return "H"
    return "R"


class CorporateBook:
    """One book: builds a (code, venue, race_no) -> race-handle index, then prices."""

    name = "book"

    def __init__(self) -> None:
        self._idx: dict[tuple[str, str, int], Any] = {}

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def handle_for(self, race: RaceRef) -> Any | None:
        tab_v = _norm_venue(race.venue)
        exact = self._idx.get((race.code, tab_v, race.race_no))
        if exact is not None:
            return exact
        for (code, vnorm, no), h in self._idx.items():
            if code == race.code and no == race.race_no and _venue_compatible(tab_v, vnorm):
                return h
        return None


class PointsbetBook(CorporateBook):
    name = "pointsbet"

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call(
            "pointsbet_racing_meetings",
            startDate=date + "T00:00:00.000Z",
            endDate=date + "T23:59:59.000Z",
        )
        if not data:
            return
        idx: dict[tuple[str, str, int], Any] = {}
        for group in data:
            for mt in group.get("meetings", []):
                code = PB_TYPE_TO_CODE.get(mt.get("racingType"))
                if not code:
                    continue
                vnorm = _norm_venue(mt.get("venue", ""))
                for ra in mt.get("races", []):
                    rno = ra.get("raceNumber")
                    if rno is not None:
                        idx[(code, vnorm, int(rno))] = ra.get("raceId")
        self._idx = idx

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        rc = await engine.try_call("pointsbet_racing_race", raceId=handle)
        out: dict[str, dict[str, Any]] = {}
        if not rc:
            return out
        for rn in rc.get("runners", []):
            if rn.get("isScratched"):
                continue
            fl = rn.get("fluctuations") or {}
            cur = fl.get("current")
            if cur:
                out[_norm_runner(rn.get("runnerName", ""))] = {"price": cur, "open": fl.get("open")}
        return out


class SportsbetBook(CorporateBook):
    name = "sportsbet"

    async def build_index(self, engine: SportsDataEngine, date: str) -> None:
        data = await engine.try_call("sportsbet_racing_allracing", eventDate=date)
        if not data:
            return
        idx: dict[tuple[str, str, int], Any] = {}
        for d in data.get("dates", []):
            for sec in d.get("sections", []):
                for mt in sec.get("meetings", []):
                    code = _sb_code(mt.get("className", ""))
                    vnorm = _norm_venue(mt.get("name", ""))
                    for ev in mt.get("events", []):
                        rno = ev.get("raceNumber")
                        if rno is not None:
                            idx[(code, vnorm, int(rno))] = ev.get("id")
        self._idx = idx

    async def prices(self, engine: SportsDataEngine, handle: Any) -> dict[str, dict[str, Any]]:
        rc = await engine.try_call("sportsbet_racecard", eventId=handle)
        out: dict[str, dict[str, Any]] = {}
        if not rc:
            return out
        markets = rc.get("markets", [])
        win = next((m for m in markets if "win" in (m.get("name", "").lower())), None)
        if win is None:
            win = markets[0] if markets else None
        if win is None:
            return out
        for s in win.get("selections", []):
            if s.get("isOut"):
                continue
            flucs = s.get("recentOddsFluctuations") or []
            price = flucs[0] if flucs else None
            if price:
                out[_norm_runner(s.get("name", ""))] = {"price": price, "open": flucs[-1] if flucs else None}
        return out


class CorporateSource:
    """Holds the enabled books, refreshes their indices, and merges prices in."""

    def __init__(self, books: list[CorporateBook] | None = None) -> None:
        self.books = books if books is not None else [PointsbetBook(), SportsbetBook()]
        self._cache: dict[str, dict[str, dict[str, float]]] = {}  # race_key -> runner -> book -> price
        self._last_fetch: dict[str, float] = {}

    async def refresh_indices(self, engine: SportsDataEngine, date: str) -> None:
        for book in self.books:
            try:
                await book.build_index(engine, date)
            except Exception as exc:
                print(f"[corporate] {book.name} index error: {exc}")

    async def enrich(self, engine: SportsDataEngine, race: RaceRef, snapshot: RaceSnapshot) -> None:
        """Fetch (throttled) and apply corporate prices onto a snapshot's runners."""
        now = time.time()
        due = now - self._last_fetch.get(race.race_key, 0) >= settings.corp_interval
        if due:
            merged: dict[str, dict[str, float]] = {}
            for book in self.books:
                handle = book.handle_for(race)
                if handle is None:
                    continue
                try:
                    prices = await book.prices(engine, handle)
                except Exception:
                    continue
                for runner_norm, p in prices.items():
                    merged.setdefault(runner_norm, {})[book.name] = p["price"]
            if merged:
                self._cache[race.race_key] = merged
                self._last_fetch[race.race_key] = now

        cache = self._cache.get(race.race_key)
        if not cache:
            return
        for r in snapshot.runners:
            books = cache.get(_norm_runner(r.name))
            if not books:
                continue
            r.corp = dict(books)
            best_book, best_price = max(books.items(), key=lambda kv: kv[1])
            r.corp_best = best_price
            r.corp_best_book = best_book

    def prune(self, keep_keys: set[str]) -> None:
        for key in list(self._cache):
            if key not in keep_keys:
                self._cache.pop(key, None)
                self._last_fetch.pop(key, None)
