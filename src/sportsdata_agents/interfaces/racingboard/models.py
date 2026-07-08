"""Shared data models for races, runners and money-flow snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RaceRef:
    """Canonical identity of a race, sourced from the TAB meetings spine."""

    race_key: str          # stable id: "{code}:{venue_mnem}:{race_no}:{date}"
    code: str              # R | G | H
    venue: str             # display name, e.g. "BATHURST"
    venue_mnem: str        # TAB mnemonic, e.g. "BAT"
    race_no: int
    race_name: str
    start_time: str        # ISO8601
    date: str              # YYYY-MM-DD

    # Optional cross-book handles filled in during enrichment.
    betfair_market_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunnerFlow:
    """Per-runner money picture at one point in time, merged across sources."""

    number: int
    name: str
    scratched: bool = False

    # TAB tote (pari-mutuel) — the pool-money signal (all codes).
    tote_win: float | None = None          # tote dividend (decimal)
    tote_pool_share: float | None = None   # normalised share of win pool [0..1]

    # TAB fixed odds.
    fixed_win: float | None = None

    # Corporate fixed odds (Sportsbet, Pointsbet, …) — book -> win price, plus
    # the best (highest) price on offer across books.
    corp: dict[str, float] = field(default_factory=dict)
    corp_best: float | None = None
    corp_best_book: str | None = None

    # Fair price (de-vigged from the sharpest market — Betfair, else tote) and the
    # value edge of the best available book price vs that fair price (%; >0 = value).
    fair_price: float | None = None
    value_pct: float | None = None

    # Betfair exchange (horses/greys).
    bf_back: float | None = None
    bf_lay: float | None = None
    bf_last: float | None = None
    bf_wom: float | None = None            # weight of money, back$/(back$+lay$) [0..1]
    bf_implied: float | None = None        # implied prob from mid price [0..1]

    # Derived movement (filled by the store from history).
    share_open: float | None = None        # first observed pool share
    share_delta: float | None = None       # current - open (pool share pts)
    price_move_pct: float | None = None     # fixed/tote drift since open (%; <0 = firming)
    direction: str = "flat"                # firming | drifting | flat

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RaceSnapshot:
    """One timestamped observation of a whole race."""

    ts: float                              # epoch seconds
    runners: list[RunnerFlow] = field(default_factory=list)

    # Race-level money aggregates.
    tote_win_pool: float | None = None     # gross win pool ($) if TAB reports it
    bf_total_matched: float | None = None  # Betfair matched on the WIN market ($)
    status: str = "OPEN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "status": self.status,
            "tote_win_pool": self.tote_win_pool,
            "bf_total_matched": self.bf_total_matched,
            "runners": [r.to_dict() for r in self.runners],
        }
