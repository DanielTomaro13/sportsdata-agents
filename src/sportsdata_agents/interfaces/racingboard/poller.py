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
import random
import time
from datetime import datetime, timezone

from .betfair import BetfairClient
from .config import settings
from .corporate import CorporateSource
from .datalog import DataLogger
from .db import DB
from .engine import SportsDataEngine
from .corporate import TabBook, build_books
from .sources import (
    BetfairMatcher,
    apply_betfair_market,
    betfair_enrich,
    finalize_snapshot,
    tab_snapshot,
)
from .spine import discover_races
from .store import Store


class Poller:
    def __init__(self, store: Store, broadcast=None) -> None:
        self.store = store
        self.broadcast = broadcast  # async callable(dict) or None
        # The training store. Optional so a dev run can skip it, but on by
        # default: a board that does not record what it saw cannot be measured
        # against what happened.
        self.db = DB(settings.db_path) if settings.enable_datalog else None
        self.datalog = DataLogger(self.db) if self.db else None
        self.engine = SportsDataEngine()
        self.betfair = BetfairClient() if settings.enable_betfair else None
        self.matcher = BetfairMatcher(self.betfair) if self.betfair else None
        # TAB is a CONTRIBUTOR to the spine, not the spine. It still supplies the tote
        # pool through `tab_snapshot`, which no corporate book has, but it no longer
        # decides which races exist — that was the ceiling on coverage.
        books = build_books()
        if settings.enable_tab:
            books = [TabBook(), *books]
        self.corporate = CorporateSource(books=books) if settings.enable_corporate else None
        self._books = books
        self._active_keys: list[str] = []
        # Sportsbet gets its OWN engine, recycled on failure and by age. Their WAF
        # flags a long-lived session that has made enough requests — once flagged,
        # EVERY sportsbet call on that session 403s while a fresh session from the
        # same IP works fine (measured 1 Sep: board session 403ing at 70/min while
        # a fresh client ran 120 calls at 3/s clean). Recycling is the antidote,
        # and isolating it here means a flagged sportsbet session can never take
        # TAB's OAuth state down with it.
        self._sb_engine = SportsDataEngine()
        self._sb_engine_born = time.time()
        self._sb_fail_streak = 0
        self._sb_skip_until = 0.0
        self._sb_index_ts = 0.0
        self._sb_far_cycle = 0
        self._cycle = 0
        self._running = False

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    async def start(self) -> None:
        self._running = True
        await self._discover_once()  # prime before serving
        loops = [self._discovery_loop(), self._price_loop()]
        if self.betfair:
            loops.append(self._fast_loop())
        await asyncio.gather(*loops)

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

        # Indices first: the spine is assembled from them, so this is the one place
        # they must be fresh before anything else runs.
        if self.corporate:
            await self.corporate.refresh_indices(self.engine, date)

        races = await discover_races(self.engine, date, self._books)
        for ref in races:
            self.store.upsert_ref(ref)

        # Track the nearest-to-jump races at full cadence.
        races.sort(key=lambda r: r.start_epoch or 0)
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

        # Drop races that are well past the jump to keep memory bounded.
        keep = {r.race_key for r in races}
        self.store.prune(keep)
        if self.corporate:
            self.corporate.prune(keep)
        cov = self.corporate.coverage(active) if self.corporate else {}
        cov_s = " ".join(f"{b}={n}" for b, n in sorted(cov.items()))
        by_code: dict[str, int] = {}
        for r in races:
            by_code[r.code] = by_code.get(r.code, 0) + 1
        print(f"[discovery] {len(races)} races tracked "
              f"({' '.join(f'{c}={n}' for c, n in sorted(by_code.items()))}), "
              f"{len(active)} active | coverage {cov_s} @ {time.strftime('%H:%M:%S')}")

    # ---- prices ----

    async def _price_loop(self) -> None:
        while self._running:
            try:
                await self._poll_active()
            except Exception as exc:
                print(f"[price] error: {exc}")
            await asyncio.sleep(settings.price_interval)

    def _due_this_cycle(self, key: str) -> bool:
        """Priority bands: nearest-to-jump refreshes every cycle, the rest less often.

        A flat cap refreshed the first N races and starved everything else, which with
        a two-hour horizon means most of the board never updates. Banding spends the
        budget where the prices actually move — inside the last ten minutes — while
        still keeping the far end alive.
        """
        st = self.store.races.get(key)
        if st is None or st.ref.start_epoch is None:
            return True
        mins = (st.ref.start_epoch - time.time()) / 60.0
        if mins <= settings.band_urgent_minutes:
            return True
        if mins <= settings.band_near_minutes:
            return self._cycle % max(1, settings.band_near_divisor) == 0
        return self._cycle % max(1, settings.band_far_divisor) == 0

    async def _poll_active(self) -> None:
        self._cycle += 1
        keys = [k for k in list(self._active_keys) if self._due_this_cycle(k)]
        # Snapshot each active race concurrently (bounded by upstream rate limits
        # inside the engine / Betfair client).
        await asyncio.gather(*(self._poll_race(k) for k in keys))
        if self.broadcast:
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers()})

    async def _book_snapshot(self, ref):
        """A snapshot built from the books, for a race TAB does not carry.

        Most of the union spine is exactly that — TAB has 413 races where the books
        between them have ~1,400 — so without this the wider spine would discover races
        the board then refused to render, and coverage would look unchanged.

        There is no tote pool here: that is TAB's alone. The row carries fixed-odds
        prices, a best-price column and the market-implied fair, which is what the
        corporate columns show anyway.
        """
        from .models import RaceSnapshot, RunnerFlow

        if not self.corporate:
            return None

        merged: dict[str, dict] = {}
        sem = asyncio.Semaphore(max(1, settings.book_concurrency))

        async def one(book):
            handle = book.handle_for(ref)
            if handle is None:
                return None
            async with sem:
                try:
                    return book.name, await book.prices(self.engine, handle)
                except Exception:
                    return None

        for got in await asyncio.gather(*(one(b) for b in self.corporate.books)):
            if got is None:
                continue
            book_name, prices = got
            for key, p in prices.items():
                row = merged.setdefault(key, {"name": p.get("name") or key,
                                              "number": p.get("number"), "books": {}})
                row["books"][book_name] = p["price"]
                # Prefer any book that actually publishes a saddlecloth number.
                if row["number"] is None and p.get("number") is not None:
                    row["number"] = p["number"]

        # One horse, one row. Merging on the normalised name alone split a runner
        # in two whenever the books spelled it differently -- Globe Derby R3 showed
        # "#5 KETO" carrying only the tote beside "#5 Keto Nz" carrying the other
        # four books. That is worse than a missing price: it inflates the field, so
        # the de-vig divides by the wrong number and every fair price in the race is
        # wrong. The saddlecloth number is the identity the books agree on.
        by_number: dict[int, dict] = {}
        for key in list(merged):
            row = merged[key]
            num = row.get("number")
            if num is None:
                continue
            first = by_number.get(int(num))
            if first is None:
                by_number[int(num)] = row
                continue
            first["books"].update(row["books"])
            # Keep the fuller spelling; it is the one a person recognises.
            if len(row["name"]) > len(first["name"]):
                first["name"] = row["name"]
            merged.pop(key, None)
        if not merged:
            return None

        runners: list[RunnerFlow] = []
        # Books that give no number get a positional one, assigned in price order so it
        # is at least stable within a snapshot. It is labelling, not identity — the
        # cross-book join is by name, in `venues.norm_runner`.
        for i, (_, row) in enumerate(sorted(merged.items(), key=lambda kv: kv[0]), start=1):
            books = row["books"]
            best_book, best_price = max(books.items(), key=lambda kv: kv[1])
            runners.append(RunnerFlow(
                number=int(row["number"]) if row["number"] is not None else i,
                name=row["name"], corp=dict(books),
                corp_best=best_price, corp_best_book=best_book,
            ))
        runners.sort(key=lambda r: r.number)
        return RaceSnapshot(ts=time.time(), runners=runners)

    async def _fast_loop(self) -> None:
        """Refresh the two markets the bot actually trades on, far faster than the rest.

        The placer bets into SPORTSBET at a fair price derived from BETFAIR, so those
        two are the board's real clock; the tote is context. Both are unthrottled, and
        every active exchange market batches into ONE market_prices call, so this is
        cheap in a way the full poll can never be -- the full poll is gated by TAB's
        2.5 rps, which is 19.6s of pure serialisation across 49 races.

        The previous board ran exactly this loop at 3s and the rewrite dropped it,
        which quietly slowed the sharpest signal on the board from 3 seconds to
        twenty. This restores it and adds Sportsbet to it for races near the jump,
        where prices move fastest and where the bot is actually trying to fire.
        """
        while self._running:
            await asyncio.sleep(settings.fast_interval)
            try:
                await self._refresh_fast()
            except Exception as exc:
                print(f"[fast] error: {exc}")

    async def _refresh_fast(self) -> None:
        id_to_key: dict[str, str] = {}
        near: list[str] = []
        sb_far: list[str] = []
        now = time.time()
        for key in list(self._active_keys):
            st = self.store.races.get(key)
            if st is None or st.latest is None:
                continue
            if st.ref.betfair_market_id:
                id_to_key[st.ref.betfair_market_id] = key
            if st.ref.start_epoch is not None and \
                    (st.ref.start_epoch - now) / 60.0 <= settings.band_near_minutes:
                near.append(key)
            else:
                sb_far.append(key)

        updated: set[str] = set()

        # Betfair and Sportsbet are fetched IN PARALLEL: the whole strategy is
        # catching Sportsbet lagging a Betfair move, so the two clocks must tick
        # together — serially, the second feed's freshness is degraded by exactly
        # the first feed's latency, on every single cycle.
        async def _betfair_leg() -> None:
            if not id_to_key:
                return
            blocks = await self.betfair.market_prices(list(id_to_key))
            for et in blocks:
                for ev in et.get("eventNodes", []):
                    for mkt in ev.get("marketNodes", []):
                        key = id_to_key.get(mkt.get("marketId"))
                        st = self.store.races.get(key) if key else None
                        if not st or not st.latest:
                            continue
                        apply_betfair_market(st.latest, mkt)
                        updated.add(key)

        # Sportsbet for races near the jump — ONE batched MultipleRacecards call
        # per 20 races, mirroring how Betfair batches into one market_prices call
        # above. This used to run the full corporate `enrich` per near race, which
        # meant a TAB request per race behind TAB's 2.5 rps throttle: with ~33
        # near races the "2-second" loop actually cycled every ~21 SECONDS, and
        # the placer bet into prices that stale — measured 1 Sep as a -16% average
        # move on every mid-placement rejection. Sportsbet is the book the bot
        # strikes, so it is the one that must be fresh; TAB and the other books
        # stay on the main poll's clock, where the throttle belongs. Batching also
        # collapses ~11 req/s of individual racecard fetches into ~1 request per
        # cycle, which is what stops Sportsbet's bot-detection 403s (2,064/day
        # before this).
        async def _sportsbet_leg() -> None:
            if not self.corporate or time.time() < self._sb_skip_until:
                return
            sb = next((b for b in self.corporate.books if b.name == "sportsbet"), None)
            if sb is None:
                return

            # Recycle the dedicated session by age; a flagged session 403s
            # everything until replaced, and replacing an unflagged one is free.
            # 300s, not 600: at the 0.5s cycle a session makes ~4 req/s, and a
            # shorter life keeps each one's request count well under wherever the
            # WAF's flag threshold lives.
            if time.time() - self._sb_engine_born > 300:
                self._sb_engine = SportsDataEngine()
                self._sb_engine_born = time.time()

            # Keep the event-id index alive on OUR session too: discovery rebuilds
            # it on the shared engine, and if that session gets flagged the index
            # quietly fossilises — old ids keep resolving, tomorrow's never appear.
            if time.time() - self._sb_index_ts > 900:
                try:
                    # LOCAL date, same as discovery: Sportsbet's eventDate is an
                    # AEST card, and the UTC date is yesterday until 10am -- an
                    # index built on it after midnight replaces today's card
                    # with resulted races until discovery wins the fight back.
                    await sb.build_index(self._sb_engine, self._today())
                    self._sb_index_ts = time.time()
                except Exception as exc:
                    print(f"[fast] sportsbet index rebuild failed: {exc}")

            # Near races every cycle; far races every 10th cycle. ALL of it goes
            # through the batch endpoint — the individual-racecard endpoint is
            # what earned the WAF flag and the board no longer touches it.
            self._sb_far_cycle += 1
            targets = list(near)
            if self._sb_far_cycle % 10 == 0:
                targets += sb_far
            id_to_race: dict[str, str] = {}
            for key in targets:
                st = self.store.races.get(key)
                if st is None or st.latest is None:
                    continue
                h = sb.handle_for(st.ref)
                if h is not None:
                    id_to_race[str(h)] = key

            failures = [0]

            async def one_chunk(chunk: list[str]) -> None:
                # Shuffle so the query string differs every cycle: the engine
                # caches identical GETs for 60s, and a cache hit here is a stale
                # price wearing a fresh timestamp — the exact thing this loop
                # exists to prevent (Galloping Jessie sat at a frozen $11 while
                # the real price was $9.50).
                random.shuffle(chunk)
                try:
                    data = await self._sb_engine.try_call(
                        "sportsbet_multiple_racecards", eventIds=",".join(chunk))
                except Exception as exc:
                    failures[0] += 1
                    if self._sb_fail_streak == 0:
                        print(f"[fast] sportsbet chunk failed: {str(exc)[:120]}")
                    return
                for evd in (data or {}).get("events", []):
                    key = id_to_race.get(str(evd.get("id")))
                    st = self.store.races.get(key) if key else None
                    if st is None or st.latest is None:
                        continue
                    prices = sb.parse_win(evd)
                    if prices:
                        self.corporate.apply_book(key, "sportsbet", prices, st.latest)
                        updated.add(key)

            ids = list(id_to_race)
            # Chunks run concurrently (endpoint caps 20 ids/batch); the cycle
            # costs the slowest request, not the sum.
            await asyncio.gather(*(one_chunk(ids[i:i + 20])
                                   for i in range(0, len(ids), 20)))

            if failures[0]:
                # Failing loudly and RECYCLING is the whole play: a flagged
                # session never recovers on its own, and silent retries against
                # it are how the board served a frozen price for an hour.
                self._sb_fail_streak += 1
                self._sb_engine = SportsDataEngine()
                self._sb_engine_born = time.time()
                if self._sb_fail_streak >= 3:
                    # Back off briefly so a genuinely angry WAF sees quiet, not
                    # a fresh session every second.
                    self._sb_skip_until = time.time() + 15
                    print(f"[fast] sportsbet leg backing off 15s "
                          f"(streak {self._sb_fail_streak})")
            else:
                if self._sb_fail_streak:
                    print(f"[fast] sportsbet leg recovered "
                          f"(after streak {self._sb_fail_streak})")
                self._sb_fail_streak = 0

        # One leg failing must not cost the other's already-applied updates.
        for r in await asyncio.gather(_betfair_leg(), _sportsbet_leg(),
                                      return_exceptions=True):
            if isinstance(r, Exception):
                print(f"[fast] leg error: {r}")

        self._fast_cycle = getattr(self, "_fast_cycle", 0) + 1
        if self._fast_cycle % 30 == 0:
            print(f"[fast] markets={len(id_to_key)} near={len(near)} "
                  f"updated={len(updated)} @ {time.strftime('%H:%M:%S')}")

        for key in updated:
            st = self.store.races.get(key)
            if st and st.latest:
                finalize_snapshot(st.latest)   # fair/value depend on the bf mids
        if self.broadcast and updated:
            # Same payload the price loop sends. The old board's Store had value()
            # and firm(); this one does not, and reaching for them threw on every
            # tick -- after the prices had already been applied, so the board stayed
            # correct while the websocket push silently died two times a second.
            await self.broadcast({"type": "board", "board": self.store.board(),
                                  "movers": self.store.movers()})

    def _tab_due(self, st) -> bool:
        """Should we spend a TAB call on this race THIS cycle?

        TAB is the only rate-limited source on the board -- 2.5 rps by its own spec,
        because it is the one feed behind an authenticated Akamai handshake -- and it
        sits on the critical path of every race it carries, with the unthrottled books
        and Betfair queued behind it. With 49 races in the horizon that is 19.6s of
        pure serialisation, and it showed: a race two minutes from the jump was
        refreshing every 21.7s against a price_interval of 8.

        The asymmetry that makes this safe is that the tote is the slowest thing TAB
        gives us. A pool share is a total of money already bet; the fixed odds and the
        exchange are what move in the last minutes. So the tote is refreshed every
        cycle where it is actually changing fast -- inside the urgent band, and the
        first time we see a race -- and every few cycles elsewhere, with the previous
        share carried forward in between.
        """
        if st.latest is None or st.ref.start_epoch is None:
            return True                      # never seen it, or cannot tell: fetch
        mins = (st.ref.start_epoch - time.time()) / 60.0
        if mins <= settings.band_urgent_minutes:
            return True
        return self._cycle % max(1, settings.tab_far_divisor) == 0

    @staticmethod
    def _carry_tote(previous, snap) -> None:
        """Copy the last tote reading onto a book-built snapshot, by runner number."""
        was = {r.number: r for r in previous.runners}
        for runner in snap.runners:
            old = was.get(runner.number)
            if old is None:
                continue
            if runner.tote_win is None:
                runner.tote_win = old.tote_win
            if runner.tote_pool_share is None:
                runner.tote_pool_share = old.tote_pool_share
        if getattr(snap, "tote_win_pool", None) is None:
            snap.tote_win_pool = getattr(previous, "tote_win_pool", None)

    async def _poll_race(self, race_key: str) -> None:
        st = self.store.races.get(race_key)
        if st is None:
            return
        ref = st.ref

        snap = None
        if settings.enable_tab and ref.venue_mnem and self._tab_due(st):
            snap = await tab_snapshot(self.engine, ref)
        if snap is None and settings.enable_tab and ref.venue_mnem and st.latest:
            # TAB was skipped this cycle, not absent. Build from the books and carry
            # the tote forward, so the board keeps a pool share rather than blinking
            # it out between refreshes.
            snap = await self._book_snapshot(ref)
            if snap is not None:
                self._carry_tote(st.latest, snap)
        if snap is None:
            # TAB does not have this race — most of the union spine is exactly that.
            # Build the runner list from the books instead, or the board could still
            # only ever show races TAB carries, which is the ceiling this removes.
            snap = await self._book_snapshot(ref)
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

        # the sportsdata racing engine's form opinion, when the warehouse has
        # one — degrades to nothing (market fair only) when it doesn't
        try:
            from .engine_fair import engine_prices

            probs = await engine_prices(date=ref.date, code=ref.code,
                                        venue_mnem=ref.venue_mnem,
                                        race_no=ref.race_no)
            for runner in snap.runners:
                if runner.number in probs:
                    runner.engine_prob = probs[runner.number]
        except Exception:
            pass

        finalize_snapshot(snap)
        self.store.add_snapshot(race_key, snap)

        detail = self.store.race_detail(race_key)
        if detail:
            # Recording must never break the poll cycle for a race.
            if self.datalog:
                try:
                    self.datalog.observe(race_key, detail)
                except Exception as exc:
                    print(f"[datalog] observe error for {race_key}: {exc}")
            if self.broadcast:
                await self.broadcast({"type": "race", "race_key": race_key, "detail": detail})
