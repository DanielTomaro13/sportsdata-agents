"""Phase A — the gate between a candidate bet and real money.

Since the blunt name-based money-tool ban was lifted, this policy is the thing standing
between a scanner's opinion and a stake. So these tests are less about "does the code
run" and more about "can it be talked into something it should refuse".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sportsdata_agents.betting.policy import (
    BettingPolicy,
    Verdict,
    load_policy,
    save_policy,
)

pytestmark = pytest.mark.unit

NOON = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)


def auto(book: str = "sportsbet", **kw) -> BettingPolicy:
    """A policy that WILL place, so a test that expects a refusal is testing the rule it
    names rather than the timid defaults."""
    return BettingPolicy(book_modes={book: "auto"}, books=[book], **kw)


# ─── the defaults are inert ─────────────────────────────────────────────


def test_the_default_policy_places_nothing() -> None:
    """A fresh policy runs the whole pipeline and touches no money. Anyone who
    constructs one without reading the docs gets paper mode."""
    d = BettingPolicy().decide(book="sportsbet", edge=0.50, odds=3.0, now=NOON)
    assert d.verdict is Verdict.PAPER
    assert d.stake and d.stake > 0  # it still sizes, so the paper trail is meaningful


# ─── the two rules that cannot be configured away ───────────────────────


@pytest.mark.parametrize("book", ["unibet", "entain"])
def test_an_unverified_book_cannot_be_set_to_auto(book: str) -> None:
    """Unibet's and Entain's placement paths were captured from real browser bets but
    never driven headlessly, so nobody knows whether the stored credential alone is
    accepted. Unattended placement there is a guess with money on it."""
    with pytest.raises(ValueError, match="round-tripped"):
        BettingPolicy(book_modes={book: "auto"})


@pytest.mark.parametrize("book", ["sportsbet", "tab"])
def test_a_verified_book_may_be_auto(book: str) -> None:
    BettingPolicy(book_modes={book: "auto"})  # does not raise


def test_an_unverified_book_may_still_ask() -> None:
    """`ask` is how a book graduates — a human watches one go through."""
    p = BettingPolicy(book_modes={"unibet": "ask"}, books=["unibet"])
    assert p.decide(book="unibet", edge=0.10, odds=3.0, now=NOON).verdict is Verdict.ASK


def test_a_global_auto_cannot_smuggle_in_an_unverified_book() -> None:
    """The per-book check is not enough on its own: `mode="auto"` with no book list
    would apply to every known book, unverified ones included."""
    with pytest.raises(ValueError, match="unproven"):
        BettingPolicy(mode="auto")


def test_a_global_auto_is_fine_when_scoped_to_verified_books() -> None:
    BettingPolicy(mode="auto", books=["sportsbet", "tab"])  # does not raise


def test_zero_edge_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="min_ev must be positive"):
        BettingPolicy(min_ev=0.0)
    with pytest.raises(ValueError, match="min_ev must be positive"):
        BettingPolicy(min_ev=-0.01)


# ─── the edge floor ─────────────────────────────────────────────────────


def test_below_the_floor_is_skipped_and_says_why() -> None:
    d = auto().decide(book="sportsbet", edge=0.018, odds=3.0, now=NOON)
    assert d.verdict is Verdict.SKIP
    assert "1.80%" in d.reason and "3.00%" in d.reason


def test_at_the_floor_it_places() -> None:
    assert auto(min_ev=0.03).decide(book="sportsbet", edge=0.03, odds=3.0, now=NOON).verdict is Verdict.PLACE


def test_a_price_that_is_not_a_price_is_refused() -> None:
    assert auto().decide(book="sportsbet", edge=0.5, odds=1.0, now=NOON).verdict is Verdict.SKIP


# ─── sizing, and the cap that outranks it ───────────────────────────────


def test_flat_sizing_stakes_the_base() -> None:
    assert auto(base_stake=3.0).size(edge=0.10, odds=3.0) == 3.0


def test_max_stake_outranks_every_sizing_rule() -> None:
    """A sizing bug that asks for the bankroll gets the ceiling instead."""
    p = auto(stake_sizing="kelly", bankroll=100_000.0, kelly_fraction=1.0, max_stake=10.0)
    assert p.size(edge=0.5, odds=3.0) == 10.0


def test_kelly_scales_with_edge() -> None:
    p = auto(stake_sizing="kelly", bankroll=1000.0, kelly_fraction=0.5, max_stake=1e9)
    small = p.size(edge=0.05, odds=3.0)
    big = p.size(edge=0.20, odds=3.0)
    assert 0 < small < big


def test_kelly_without_a_bankroll_stakes_nothing() -> None:
    """A misconfiguration, and it must not silently fall back to the flat amount."""
    p = auto(stake_sizing="kelly", bankroll=0.0)
    assert p.size(edge=0.2, odds=3.0) == 0.0
    assert p.decide(book="sportsbet", edge=0.2, odds=3.0, now=NOON).verdict is Verdict.SKIP


# ─── budget ─────────────────────────────────────────────────────────────


def test_the_daily_cap_stops_placing() -> None:
    d = auto(daily_cap=20.0).decide(book="sportsbet", edge=0.2, odds=3.0, now=NOON, staked_today=20.0)
    assert d.verdict is Verdict.SKIP and "daily cap" in d.reason


def test_a_bet_is_trimmed_to_what_is_left_of_the_cap() -> None:
    d = auto(base_stake=10.0, daily_cap=20.0).decide(
        book="sportsbet", edge=0.2, odds=3.0, now=NOON, staked_today=17.0
    )
    assert d.verdict is Verdict.PLACE and d.stake == 3.0


def test_open_exposure_is_a_separate_ceiling() -> None:
    d = auto(max_open_exposure=50.0).decide(
        book="sportsbet", edge=0.2, odds=3.0, now=NOON, open_exposure=50.0
    )
    assert d.verdict is Verdict.SKIP and "exposure" in d.reason


def test_the_daily_bet_count_is_capped() -> None:
    d = auto(max_bets_per_day=3).decide(book="sportsbet", edge=0.2, odds=3.0, now=NOON, bets_today=3)
    assert d.verdict is Verdict.SKIP and "cap is 3" in d.reason


def test_paper_mode_reports_the_same_refusals_a_live_run_would_hit() -> None:
    """A paper trail that ignores the caps teaches nothing about whether they are set
    right — so budget is checked before mode, not after."""
    d = BettingPolicy(daily_cap=20.0).decide(
        book="sportsbet", edge=0.2, odds=3.0, now=NOON, staked_today=20.0
    )
    assert d.verdict is Verdict.SKIP and "daily cap" in d.reason


# ─── routing and per-book modes ─────────────────────────────────────────


def test_a_book_off_the_list_is_skipped() -> None:
    p = BettingPolicy(books=["sportsbet"], book_modes={"sportsbet": "auto"})
    assert p.decide(book="tab", edge=0.2, odds=3.0, now=NOON).verdict is Verdict.SKIP


def test_never_means_never() -> None:
    p = BettingPolicy(book_modes={"tab": "never"}, mode="paper")
    assert p.decide(book="tab", edge=0.9, odds=3.0, now=NOON).verdict is Verdict.SKIP


def test_per_book_mode_overrides_the_global_one() -> None:
    p = BettingPolicy(mode="paper", book_modes={"sportsbet": "auto"})
    assert p.decide(book="sportsbet", edge=0.2, odds=3.0, now=NOON).verdict is Verdict.PLACE
    assert p.decide(book="tab", edge=0.2, odds=3.0, now=NOON).verdict is Verdict.PAPER


def test_an_unknown_book_is_refused_not_guessed() -> None:
    assert BettingPolicy().decide(book="ladbrokes_nz", edge=0.2, odds=3.0, now=NOON).verdict is Verdict.SKIP
    with pytest.raises(ValueError, match="unknown book"):
        BettingPolicy(books=["ladbrokes_nz"])


# ─── quiet hours ────────────────────────────────────────────────────────


def test_quiet_hours_defer_rather_than_discard() -> None:
    d = auto().decide(book="sportsbet", edge=0.2, odds=3.0, now=MIDNIGHT)
    assert d.verdict is Verdict.ASK
    assert d.deferred is True
    assert d.stake  # the sizing survives, so a human can approve it as-is


def test_quiet_hours_wrap_midnight() -> None:
    p = auto()
    assert p.in_quiet_hours(datetime(2026, 8, 27, 23, 30, tzinfo=UTC))
    assert p.in_quiet_hours(datetime(2026, 8, 27, 3, 0, tzinfo=UTC))
    assert not p.in_quiet_hours(NOON)


def test_quiet_hours_can_be_turned_off() -> None:
    p = auto(quiet_hours=None)
    assert p.decide(book="sportsbet", edge=0.2, odds=3.0, now=MIDNIGHT).verdict is Verdict.PLACE


# ─── persistence ────────────────────────────────────────────────────────


def test_a_policy_round_trips(tmp_path) -> None:
    p = auto(base_stake=2.5, min_ev=0.07)
    path = tmp_path / "policy.json"
    save_policy(p, path)
    back = load_policy(path)
    assert back.base_stake == 2.5 and back.min_ev == 0.07
    assert back.quiet_hours == p.quiet_hours  # tuple survives the JSON list round-trip


def test_a_saved_policy_that_breaks_a_rule_is_rejected_on_load(tmp_path) -> None:
    """Editing the file by hand — or an agent editing it — must not be a way around the
    unverified-book rule."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"book_modes": {"unibet": "auto"}}))
    with pytest.raises(ValueError, match="round-tripped"):
        load_policy(path)
