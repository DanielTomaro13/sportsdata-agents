"""Shared data models for races, runners and money-flow snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RaceRef:
    """Canonical identity of a race, from the UNION of every book's card.

    Identity is `(code, venue, race_no, date)` with the venue resolved through
    `venues.py`. It deliberately no longer depends on TAB's `venueMnemonic`: that does
    not exist for a race TAB does not carry, so keying on it capped the board at TAB's
    card — the smallest of the five. The mnemonic survives only as TAB's own handle,
    and is None for the many races TAB does not have.
    """

    race_key: str          # stable id: "{code}:{venue_key}:{race_no}:{date}"
    code: str              # R | G | H
    venue: str             # display name, the most informative spelling seen
    venue_mnem: str | None  # TAB mnemonic, e.g. "BAT" — None when TAB lacks the race
    race_no: int
    race_name: str
    start_time: str        # ISO8601
    date: str              # YYYY-MM-DD

    #: Advertised start as epoch seconds. First-class rather than derived, because it
    #: is the join key for books that publish no race number (Dabble).
    start_epoch: float | None = None

    #: Which books contributed this race to the spine — the coverage signal.
    books: list[str] = field(default_factory=list)

    #: TAB's meeting location: an AU state (NSW/VIC/...) or a country code. Empty
    #: for the many races TAB does not carry. The placer gates its tote blend on
    #: this, and that is consistent: no TAB race means no TAB pool to blend.
    location: str = ""

    # Optional cross-book handles filled in during enrichment.
    betfair_market_id: str | None = None

    @property
    def venue_key(self) -> str:
        from .venues import norm_venue

        return norm_venue(self.venue)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["venue_key"] = self.venue_key
        return d


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
    # TAB's proposition number for this runner's WIN price. It is the key the
    # push websocket (push.beta.tab.com.au /odds-update) subscribes on, so the
    # board captures it here purely to hand downstream consumers (the placer)
    # what they need to open a real-time TAB price stream. The board itself does
    # not use it.
    tab_prop: int | None = None

    # Corporate fixed odds (Sportsbet, Pointsbet, …) — book -> win price, plus
    # the best (highest) price on offer across books.
    corp: dict[str, float] = field(default_factory=dict)
    corp_best: float | None = None
    corp_best_book: str | None = None

    # The sportsdata racing engine's form win probability, when the warehouse
    # has one for this runner (else None → board falls back to market fair).
    engine_prob: float | None = None

    # Fair price and the value edge of the best available book price vs it
    # (%; >0 = value). fair_source names where the fair came from:
    # "engine" (form model) | "betfair" (de-vigged exchange) | "tote" (pool).
    fair_price: float | None = None
    fair_source: str | None = None
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

    # When the fast loop last applied a REAL Sportsbet quote to this race.
    # 0.0 means never: the race's sportsbet price, if any, came from discovery
    # and has not been refreshed -- exactly the state that held CHARLIE MASON
    # (Ripon, 1 Sep) at a frozen $11 for 7+ minutes while Sportsbet quoted
    # $9.50. The placer refuses to strike any quote older than its freshness
    # window, so a race the batch cannot venue-match simply never gets bet.
    sb_ts: float = 0.0

    # Race-level money aggregates.
    tote_win_pool: float | None = None     # gross win pool ($) if TAB reports it
    results: list[int] | None = None       # finishing order (runner numbers) once run
    winners: list[int] | None = None       # the whole first group — >1 on a dead-heat
    bf_total_matched: float | None = None  # Betfair matched on the WIN market ($)
    status: str = "OPEN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "sb_ts": self.sb_ts,
            "status": self.status,
            "tote_win_pool": self.tote_win_pool,
            "bf_total_matched": self.bf_total_matched,
            "runners": [r.to_dict() for r in self.runners],
        }
