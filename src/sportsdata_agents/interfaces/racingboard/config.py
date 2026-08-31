"""Runtime configuration for the racing money-flow tool.

Everything is overridable via environment variables so the same code runs on a
laptop or a box. The one path that matters is SPORTSDATA_MCP_SRC — the `src`
directory of your local sportsdata-mcp checkout, whose vetted HTTP engine we
import as a library to reach TAB (Akamai-gated) and the corporate books.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_mcp_src() -> str:
    # Sensible default for this machine; override with SPORTSDATA_MCP_SRC.
    guess = Path.home() / "Documents" / "Projects" / "sportsdata-mcp" / "src"
    return os.environ.get("SPORTSDATA_MCP_SRC", str(guess))


@dataclass
class Settings:
    # --- sportsdata-mcp engine (TAB + corporate data layer) ---
    sportsdata_mcp_src: str = field(default_factory=_default_mcp_src)

    # --- polling cadence (seconds) ---
    # Board (all upcoming races) is discovered less often than prices are polled.
    discovery_interval: float = float(os.environ.get("MF_DISCOVERY_INTERVAL", "60"))
    price_interval: float = float(os.environ.get("MF_PRICE_INTERVAL", "8"))
    # Corporate books rate-limit, so price them on a slower cadence than the tote.
    corp_interval: float = float(os.environ.get("MF_CORP_INTERVAL", "20"))

    # How far ahead to track races for the board (minutes to jump). Two hours: the
    # requirement is that everything inside it is polled hard, which is 41 races at a
    # quiet moment and 86 at the busiest rolling window of the day.
    horizon_minutes: int = int(os.environ.get("MF_HORIZON_MINUTES", "120"))

    # --- training store -------------------------------------------------------
    # The board is also the record: every snapshot and every result lands in
    # sqlite so calibration, the firm model and the placer's analysis have a
    # history to work from. Without this the board is a screen and nothing else.
    db_path: str = os.environ.get("MF_DB_PATH", "/var/lib/racingboard/racingboard.db")
    enable_datalog: bool = os.environ.get("MF_DATALOG", "1") == "1"
    datalog_buckets: tuple[int, ...] = tuple(
        int(x) for x in os.environ.get("MF_DATALOG_BUCKETS", "120,90,60,45,30,20,15,10,5,2").split(","))
    # Firm label threshold: open→jump shortened by ≥ this fraction ⇒ firmed.
    firm_threshold: float = float(os.environ.get("MF_FIRM_THRESHOLD", "0.08"))
    # Max races polled at full cadence at once (protects the upstreams).
    max_active_races: int = int(os.environ.get("MF_MAX_ACTIVE_RACES", "120"))

    # --- priority bands (minutes to jump) ---
    # When the budget cannot refresh everything inside the horizon, nearest-to-jump
    # wins. A single global cap starves the far band entirely, which is what a flat
    # `max_active_races` of 12 did to everything beyond a few minutes out.
    band_urgent_minutes: int = int(os.environ.get("MF_BAND_URGENT", "10"))
    band_near_minutes: int = int(os.environ.get("MF_BAND_NEAR", "30"))
    #: Refresh multiplier per band: urgent every cycle, near every 2nd, far every 4th.
    band_near_divisor: int = int(os.environ.get("MF_BAND_NEAR_DIVISOR", "2"))
    band_far_divisor: int = int(os.environ.get("MF_BAND_FAR_DIVISOR", "4"))
    # How often a race outside the urgent band spends a TAB call. TAB is the only
    # throttled source (2.5 rps, authenticated) and sits on every race's critical
    # path, so this is the single biggest lever on how fresh the board is.
    tab_far_divisor: int = int(os.environ.get("MF_TAB_FAR_DIVISOR", "6"))
    # How often Betfair and Sportsbet refresh -- the two markets the bot trades on,
    # both unthrottled, batched into one exchange call. This is the board's real
    # clock; price_interval only governs the tote-bound full sweep.
    fast_interval: float = float(os.environ.get("MF_FAST_INTERVAL", "2"))

    # --- the spine ---
    # Books contributing races AND prices. TAB is always a contributor and is added
    # separately; these are the corporate books.
    books: tuple[str, ...] = tuple(
        b.strip() for b in os.environ.get(
            "MF_BOOKS", "pointsbet,sportsbet,ladbrokes,dabble").split(",") if b.strip()
    )
    #: How far apart two books' advertised starts may be and still be one race. Books
    #: disagree slightly on a scheduled time; this is the tolerance for the start-time
    #: join used by books that publish no race number.
    start_tolerance_seconds: int = int(os.environ.get("MF_START_TOLERANCE", "180"))
    #: Grace window after the jump, so a race stays visible through the moment it runs.
    #: How long a race stays on the board AFTER it jumps. Thirty minutes, not two.
    #:
    #: Results post several minutes after the off, and everything downstream needs
    #: the race to still be there when they do: the placer grades a bet by asking
    #: the board whether its race is RESULTED, and the training store writes its
    #: outcome the same way. At two minutes the race vanished first, so a confirmed
    #: bet on Corowa R1 was not slow to settle -- it could never settle at all, and
    #: neither could the outcome row behind it.
    #:
    #: The previous board used 1800 for exactly this reason and said so in a comment
    #: that survived the rewrite while the number did not.
    past_grace_seconds: int = int(os.environ.get("MF_PAST_GRACE", "1800"))
    # How many books to price CONCURRENTLY within one race. Books are independent
    # upstreams, so walking them serially made a race cost the SUM of their latencies
    # (~0.47s over five books) when it need only cost the slowest (~0.26s, Sportsbet).
    # Bounded rather than unbounded so adding books cannot silently multiply the
    # instantaneous request rate against any one of them.
    book_concurrency: int = int(os.environ.get("MF_BOOK_CONCURRENCY", "8"))

    # --- upstream throttling ---
    # The books the board may hit AT FULL TILT. The sportsdata-mcp engine applies a
    # per-provider token bucket (default 10 rps, and some specs set less); the board
    # overrides it for these books only, on its OWN engine instance, so nothing else
    # that talks to the MCP is affected.
    #
    # TAB IS DELIBERATELY ABSENT AND MUST STAY ABSENT. It is the one source here reached
    # through an authenticated Akamai handshake rather than an anonymous public feed, so
    # it is the one where hitting hard is a real account risk rather than a bandwidth
    # question. It keeps its spec limit (2.5 rps). Adding "tab" to this tuple would
    # silently remove that protection, which is why the set is named rather than derived
    # as "everything except".
    unthrottled_books: tuple[str, ...] = tuple(
        b.strip() for b in os.environ.get(
            "MF_UNTHROTTLED_BOOKS", "pointsbet,sportsbet,entain,dabble").split(",")
        if b.strip() and b.strip() != "tab"
    )
    # Sustained rate and burst applied to each of the above. High by intent: these are
    # anonymous public racecard feeds and the board's whole job is fresh prices.
    #
    # 200 is where the measured curve flattens, not a guess. Sweeping 120 real price
    # requests across PointsBet + Sportsbet (2026-08-31, never_cache so every call hits
    # the network):
    #
    #     rps=  10 ->  23 req/s      rps= 200 -> 310 req/s
    #     rps=  50 -> 237 req/s      rps=1000 -> 234 req/s
    #
    # Past ~200 the token bucket is no longer the constraint — the connection pool is —
    # so a larger number buys nothing and only widens the blast radius if a book starts
    # objecting. Runner counts were identical (312) and errors zero at every setting.
    book_rps: float = float(os.environ.get("MF_BOOK_RPS", "200"))
    book_burst: int = int(os.environ.get("MF_BOOK_BURST", "200"))

    # TAB jurisdiction for the meetings spine.
    jurisdiction: str = os.environ.get("MF_JURISDICTION", "NSW")

    # Racing codes to track: R=thoroughbred, G=greyhound, H=harness.
    codes: tuple[str, ...] = tuple(os.environ.get("MF_CODES", "R,G,H").split(","))

    # --- source toggles ---
    enable_betfair: bool = os.environ.get("MF_BETFAIR", "1") == "1"
    enable_tab: bool = os.environ.get("MF_TAB", "1") == "1"
    enable_corporate: bool = os.environ.get("MF_CORPORATE", "1") == "1"

    # Time-series retention per race (number of snapshots kept in memory).
    history_len: int = int(os.environ.get("MF_HISTORY_LEN", "300"))

    # HTTP server.
    host: str = os.environ.get("MF_HOST", "127.0.0.1")
    # Honour a harness-assigned PORT (preview/hosting) before MF_PORT/default.
    port: int = int(os.environ.get("PORT") or os.environ.get("MF_PORT") or "8000")


settings = Settings()

CODE_LABEL = {"R": "Thoroughbred", "G": "Greyhound", "H": "Harness"}
