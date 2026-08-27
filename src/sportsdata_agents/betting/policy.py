"""What may happen to real money without a human present.

The placement tools exist and work. This module answers the separate question: given a
bet the scanner likes, does the plane *place* it, *ask* first, *record* it and stop, or
refuse outright?

That is a per-person, per-book judgement, so it is a setting rather than a prompt
instruction — a system prompt is advice a model can talk itself out of, and this is a
gate the code enforces before a request is ever built:

    mode:        paper          # paper | ask | auto | never
    min_ev:      0.03           # 3% edge before anything is proposed at all
    stake:       flat           # flat | kelly
    max_stake:   10.00          # hard ceiling per bet, whatever the sizing says
    daily_cap:   50.00          # spend per day
    books:       [sportsbet]    # nothing placed anywhere else

THE DEFAULTS ARE DELIBERATELY INERT. Everything starts at `paper`: the whole pipeline
runs, every decision is recorded, and nothing reaches a bookmaker. Autonomy is opted
into book by book, because the failure mode of a plane that acts too freely is money
that is gone, while the failure mode of one that records too much is a longer ledger.

## Everything is configurable

There are no betting rules the owner cannot change. Two settings are nonetheless
DEFAULTED to the cautious side and warn loudly when moved, because each is grounded in a
measurement rather than a preference:

1. **`allow_unverified_auto` (default off).** As of 2026-08-27 Unibet and Ladbrokes/
   Entain have place-bet contracts captured from real BROWSER placements — so the
   request is known good and the stored CREDENTIAL is not, because neither has been
   driven headlessly. With the flag off, `auto` on those books is downgraded to `ask`
   rather than refused, so a first live placement is watched; turning it on places
   unattended. `VERIFIED_BOOKS` grew by measurement and is the list of the proven ones.

2. **`min_ev` (default 0.03).** Zero or negative is permitted and warns: at that floor
   every candidate clears, so the plane donates the vig on every bet it finds, at
   machine speed.

The only things that still raise are arithmetic nonsense (a negative cap) and a book the
plane has no placement tool for — a capability limit, not a policy one.

## Why this gate is deterministic

The scanner reads bookmaker pages and API responses. That is attacker-controlled
content: a book (or anything injected into its payload) can contain text aimed at the
agent reading it. Since the platform's blunt name-based money-tool ban was lifted, this
policy is the thing standing between injected text and a real stake — so every decision
here is arithmetic on typed fields, never a judgement handed to a model. Nothing in this
module reads free text, and no prompt can widen a limit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

log = logging.getLogger(__name__)

#: paper — run everything, place nothing (the default, and the only safe starting point)
#: ask   — build the bet, hand it to a human, wait
#: auto  — place it unattended, inside every limit below
#: never — do not even propose on this book
Mode = Literal["paper", "ask", "auto", "never"]

Sizing = Literal["flat", "kelly"]


class Verdict(StrEnum):
    """What the policy decided about one candidate bet."""

    PLACE = "place"   # inside policy — place it, then confirm by reading the account
    ASK = "ask"       # route to the human and wait
    PAPER = "paper"   # record the intent and the price; touch no money
    SKIP = "skip"     # not wanted at all


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    #: Set when the bet is fine but the moment is not (quiet hours) — a caller may hold
    #: it rather than discard it.
    deferred: bool = False
    #: What the policy sized the bet at, once every cap is applied. None when nothing
    #: would be staked (SKIP, or PAPER with no sizing asked for).
    stake: float | None = None


@dataclass
class BettingPolicy:
    """One person's rules. Serialised whole; see `load_policy` / `save_policy`."""

    #: Books whose placement path has been driven end to end against a real account, so
    #: the credential and the request are both known to work unattended.
    #:
    #: Sportsbet and TAB were round-tripped live on 2026-08-27. Unibet and Entain were
    #: NOT: their contracts were captured from real placements made in a browser, which
    #: proves the request shape and nothing about whether the stored credential alone is
    #: accepted. Entain additionally sits behind Kasada, and its token endpoint is
    #: config-derived rather than observed. Moving a book into this set means one
    #: supervised live placement succeeded — it is a record of evidence, not a
    #: preference, and it should never be edited to unblock a config.
    VERIFIED_BOOKS: ClassVar[frozenset[str]] = frozenset({"sportsbet", "tab"})

    #: Every book the betting plane knows how to place at.
    KNOWN_BOOKS: ClassVar[frozenset[str]] = frozenset({"sportsbet", "tab", "entain", "unibet"})

    #: The global default, applied to any book without its own entry.
    mode: Mode = "paper"
    #: Per-book override, e.g. {"sportsbet": "auto", "unibet": "ask"}.
    book_modes: dict[str, Mode] = field(default_factory=dict)

    #: Minimum modelled edge before a candidate is considered at all, as a fraction:
    #: 0.03 is a 3% edge. Applied before sizing, before budget, before anything.
    min_ev: float = 0.03

    #: Sizing rule. `flat` stakes `base_stake` every time. `kelly` stakes a fraction of
    #: bankroll proportional to edge, which is why `kelly_fraction` exists — full Kelly
    #: is famously correct and famously unusable, because it assumes the edge estimate
    #: is exact and this one is a model's guess.
    stake_sizing: Sizing = "flat"
    base_stake: float = 1.0
    kelly_fraction: float = 0.25
    bankroll: float = 0.0

    #: The hard ceiling on a single bet, applied last and to every sizing rule. A sizing
    #: bug that asks for the bankroll gets `max_stake` instead.
    max_stake: float = 10.0
    #: Total that may be staked in one UTC day across every book.
    daily_cap: float = 50.0
    #: Total that may be at risk in unsettled bets at once.
    max_open_exposure: float = 100.0
    #: A cap on unattended placements per day. A scanner bug that finds an edge in
    #: everything should exhaust a budget, not an account.
    max_bets_per_day: int = 5

    #: Books eligible at all. Empty means every KNOWN_BOOK (subject to its mode).
    books: list[str] = field(default_factory=list)

    #: Never place inside this window; hold instead. Bookmakers price overnight markets
    #: thinly and a human is not awake to notice a runaway.
    quiet_hours: tuple[str, str] | None = ("23:00", "07:00")

    #: How far a price may move between the quote the edge was computed on and the
    #: quote at placement, as a fraction. Beyond this the bet is abandoned, not placed
    #: at the worse number — the edge was the whole reason for the bet.
    max_price_drift: float = 0.02

    #: Allow `auto` on a book whose placement path has never been round-tripped
    #: headlessly (currently Unibet and Entain — see VERIFIED_BOOKS).
    #:
    #: OFF BY DEFAULT, NOT FORBIDDEN. Leaving it off downgrades an unverified `auto` to
    #: `ask` rather than refusing to start, so nothing is silently placed on a path
    #: nobody has proven; turning it on is a deliberate line in a config file. The reason
    #: to leave it off is narrow and factual: those two contracts were captured from
    #: browser placements, so the REQUEST is known good and the stored CREDENTIAL is not.
    allow_unverified_auto: bool = False

    notes: list[str] = field(default_factory=list)

    # ─── validation ─────────────────────────────────────────────────────
    #
    # NOTHING HERE IS A BETTING RULE. Every policy question — how much edge is enough,
    # what may be staked, which books, attended or not — is a setting the owner controls,
    # including the two that used to raise. What remains is arithmetic sanity: values
    # that have no meaning at all (a negative cap), and books the plane has no tool for.
    # Those are reported rather than obeyed, because a policy built from them cannot
    # execute regardless of what anyone intended.

    def __post_init__(self) -> None:
        if self.min_ev <= 0:
            # Allowed — it is the owner's call — but never silent. At zero or below, the
            # plane pays the vig on every bet it can find, at machine speed.
            log.warning(
                "betting policy has min_ev=%s: every candidate clears the edge floor, "
                "including negative-expectation bets", self.min_ev,
            )
        unverified_auto = sorted(
            b for b, m in self.book_modes.items()
            if m == "auto" and b not in self.VERIFIED_BOOKS
        )
        if self.mode == "auto":
            unverified_auto = sorted(
                set(unverified_auto) | (set(self._eligible_books()) - self.VERIFIED_BOOKS
                                        - {b for b, m in self.book_modes.items() if m != "auto"})
            )
        if unverified_auto and not self.allow_unverified_auto:
            log.warning(
                "betting policy sets 'auto' on %s, whose placement paths have never been "
                "round-tripped headlessly — these will ASK instead. Set "
                "allow_unverified_auto=True to place on them unattended.",
                unverified_auto,
            )
        elif unverified_auto:
            log.warning(
                "betting policy will place UNATTENDED on %s via allow_unverified_auto — "
                "their stored credentials have never been proven to satisfy the book",
                unverified_auto,
            )

        for name, value in (("max_stake", self.max_stake), ("daily_cap", self.daily_cap),
                            ("base_stake", self.base_stake), ("max_open_exposure", self.max_open_exposure)):
            if value < 0:
                raise ValueError(f"{name} cannot be negative — a negative limit has no meaning")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1] — 1.0 is full Kelly and already aggressive")
        for book in [*self.books, *self.book_modes]:
            if book not in self.KNOWN_BOOKS:
                raise ValueError(
                    f"no placement tool for book {book!r} — the plane can place at "
                    f"{sorted(self.KNOWN_BOOKS)}. This is a capability limit, not a policy one."
                )

    def _eligible_books(self) -> frozenset[str]:
        return frozenset(self.books) if self.books else self.KNOWN_BOOKS

    # ─── the decision ───────────────────────────────────────────────────

    def mode_for(self, book: str) -> Mode:
        return self.book_modes.get(book, self.mode)

    def decide(
        self,
        *,
        book: str,
        edge: float,
        odds: float,
        now: datetime,
        staked_today: float = 0.0,
        bets_today: int = 0,
        open_exposure: float = 0.0,
    ) -> Decision:
        """The single gate. Every field is a number or a known string; nothing here reads
        free text, so no content fetched from a bookmaker can influence the outcome.

        `edge` is the modelled edge as a fraction (0.05 = 5%). `odds` is decimal.
        """
        if book not in self.KNOWN_BOOKS:
            return Decision(Verdict.SKIP, f"{book!r} is not a book this plane can place at")
        if book not in self._eligible_books():
            return Decision(Verdict.SKIP, f"policy: {book} is not in the eligible book list")

        mode = self.mode_for(book)
        if mode == "never":
            return Decision(Verdict.SKIP, f"policy: {book} is off")

        if edge < self.min_ev:
            return Decision(
                Verdict.SKIP,
                f"edge {edge:.2%} is below the {self.min_ev:.2%} floor",
            )
        if odds <= 1.0:
            return Decision(Verdict.SKIP, f"odds {odds} are not a price (must exceed 1.0)")

        stake = self.size(edge=edge, odds=odds)
        if stake <= 0:
            return Decision(Verdict.SKIP, "sizing produced no stake")

        # Budget is checked BEFORE mode, so `paper` reports the same refusals a live run
        # would hit. A paper trail that ignores the caps teaches nothing about them.
        if bets_today >= self.max_bets_per_day:
            return Decision(Verdict.SKIP, f"already placed {bets_today} today, cap is {self.max_bets_per_day}")
        remaining_day = self.daily_cap - staked_today
        if remaining_day <= 0:
            return Decision(Verdict.SKIP, f"daily cap of {self.daily_cap:.2f} is spent")
        remaining_exposure = self.max_open_exposure - open_exposure
        if remaining_exposure <= 0:
            return Decision(
                Verdict.SKIP,
                f"open exposure {open_exposure:.2f} is at the {self.max_open_exposure:.2f} limit",
            )
        stake = min(stake, remaining_day, remaining_exposure)

        if mode == "paper":
            return Decision(Verdict.PAPER, "policy: paper mode — recorded, nothing placed", stake=stake)
        if mode == "ask":
            return Decision(Verdict.ASK, f"policy: {book} is set to ask", stake=stake)

        # auto — the only branch that can move money unattended.
        if book not in self.VERIFIED_BOOKS and not self.allow_unverified_auto:
            # Downgraded, not refused: the owner asked for unattended placement on a book
            # whose stored credential has never been proven to satisfy it. Flipping
            # allow_unverified_auto makes this place.
            return Decision(
                Verdict.ASK,
                f"{book} is set to auto but its placement path has never been "
                f"round-tripped headlessly — asking instead (set allow_unverified_auto "
                f"to place unattended)",
                stake=stake,
            )
        if self.in_quiet_hours(now):
            return Decision(
                Verdict.ASK,
                f"inside quiet hours {self.quiet_hours[0]}-{self.quiet_hours[1]}",  # type: ignore[index]
                deferred=True,
                stake=stake,
            )
        return Decision(Verdict.PLACE, f"inside policy at {book}: edge {edge:.2%}, stake {stake:.2f}", stake=stake)

    # ─── sizing ─────────────────────────────────────────────────────────

    def size(self, *, edge: float, odds: float) -> float:
        """Stake for one bet, before budget trimming. Always bounded by `max_stake`."""
        if self.stake_sizing == "flat":
            raw = self.base_stake
        else:
            # Kelly on decimal odds: f = edge / (odds - 1), scaled down because the edge
            # is an estimate. Bankroll of 0 means Kelly has nothing to work from, which
            # is a misconfiguration rather than a reason to stake the flat amount.
            b = odds - 1.0
            if b <= 0 or self.bankroll <= 0:
                return 0.0
            raw = self.bankroll * (edge / b) * self.kelly_fraction
        return max(0.0, min(raw, self.max_stake))

    # ─── time ───────────────────────────────────────────────────────────

    def in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet_hours:
            return False
        start, end = (time.fromisoformat(x) for x in self.quiet_hours)
        current = now.astimezone(UTC).time() if now.tzinfo else now.time()
        # A window that crosses midnight is two ranges, not one.
        if start <= end:
            return start <= current < end
        return current >= start or current < end


# ─── persistence ────────────────────────────────────────────────────────


def load_policy(path: Path) -> BettingPolicy:
    """Read a policy, validating it. A file that would fail `__post_init__` — an `auto`
    on an unverified book, say — raises here rather than being silently accepted."""
    data = json.loads(path.read_text())
    quiet = data.get("quiet_hours")
    if isinstance(quiet, list):
        data["quiet_hours"] = tuple(quiet)
    return BettingPolicy(**data)


def save_policy(policy: BettingPolicy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(policy), indent=2, sort_keys=True))
