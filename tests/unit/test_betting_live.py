"""Reading a re-quoted price out of four different books, and scoping a session.

These readers feed the drift gate, so a reader that returns a plausible wrong number
does not fail — it places a bet at a price nobody checked. Each book's unit trap is
pinned here.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.betting import live
from sportsdata_agents.betting.live import PriceUnreadable
from sportsdata_agents.betting.policy import BettingPolicy

pytestmark = pytest.mark.unit


# ─── Sportsbet ──────────────────────────────────────────────────────────


def test_sportsbet_price_comes_from_the_combination() -> None:
    quote = {"betBuilds": [{"betCombinations": [{"betEnhancedPrice": 3.45}]}]}
    assert live.read_sportsbet_price(quote) == 3.45


def test_sportsbet_falls_back_to_the_enhanced_decimal() -> None:
    quote = {"betBuilds": [{"betCombinations": [], "enhancedOdds": [{"priceDecimal": 3.2}]}]}
    assert live.read_sportsbet_price(quote) == 3.2


def test_sportsbet_with_nothing_usable_refuses() -> None:
    with pytest.raises(PriceUnreadable):
        live.read_sportsbet_price({"betBuilds": [{}]})
    with pytest.raises(PriceUnreadable):
        live.read_sportsbet_price({})


# ─── TAB ────────────────────────────────────────────────────────────────


def test_tab_multiplies_its_legs() -> None:
    """TAB prices per leg and gives no combined figure on the slip, so the multi price
    is the product."""
    quote = {"bets": [{"legs": [{"odds": "2.00"}, {"odds": "1.70"}]}]}
    assert live.read_tab_price(quote) == pytest.approx(3.4)


def test_tab_odds_are_strings_on_the_wire() -> None:
    quote = {"bets": [{"legs": [{"odds": "2.5"}]}]}
    assert live.read_tab_price(quote) == pytest.approx(2.5)


def test_tab_with_unreadable_legs_refuses() -> None:
    with pytest.raises(PriceUnreadable):
        live.read_tab_price({"bets": [{"legs": [{"odds": "nonsense"}]}]})
    with pytest.raises(PriceUnreadable):
        live.read_tab_price({"bets": []})


# ─── Entain ─────────────────────────────────────────────────────────────


def test_entain_prefers_the_decimal_it_gives() -> None:
    quote = {"e-1": {"odds": {"numerator": 12, "denominator": 5, "decimal": 3.4}}}
    assert live.read_entain_price(quote) == 3.4


def test_entain_fractions_need_the_plus_one() -> None:
    """decimal = num/den + 1. Forgetting the +1 gives a price one short — small enough
    to look plausible, and wrong on every single bet."""
    quote = {"e-1": {"odds": {"numerator": 12, "denominator": 5}}}
    assert live.read_entain_price(quote) == pytest.approx(3.4)   # not 2.4


def test_entain_with_no_odds_refuses() -> None:
    with pytest.raises(PriceUnreadable):
        live.read_entain_price({"e-1": {}})


# ─── Unibet / Kambi ─────────────────────────────────────────────────────


def test_kambi_thousandths_are_divided() -> None:
    """3400 IS 3.40. Returning the raw number is a price a thousand times too long."""
    quote = {"couponRows": [{"odds": 3400}]}
    assert live.read_unibet_price(quote) == pytest.approx(3.4)


def test_kambis_rejection_envelope_is_not_a_price() -> None:
    """{status, message} is how Kambi refuses. Reading a price out of a refusal would
    place a bet the book already declined."""
    with pytest.raises(PriceUnreadable, match="refused"):
        live.read_unibet_price({"status": 400, "message": "coupon is stale"})


def test_kambi_with_no_rows_refuses() -> None:
    with pytest.raises(PriceUnreadable):
        live.read_unibet_price({"couponRows": []})


# ─── scoping ────────────────────────────────────────────────────────────


def test_a_paper_policy_can_place_nowhere() -> None:
    """The default. No write group means the placement tools are ABSENT from the
    session, not merely declined by the policy."""
    assert live.scope_for(BettingPolicy()) == []


def test_only_books_set_to_ask_or_auto_get_a_write_group() -> None:
    policy = BettingPolicy(book_modes={"sportsbet": "auto", "tab": "ask",
                                       "unibet": "paper", "entain": "never"})
    assert live.scope_for(policy) == ["sportsbet.write", "tab.write"]


def test_a_book_outside_the_eligible_list_gets_no_write_group() -> None:
    policy = BettingPolicy(book_modes={"sportsbet": "auto", "tab": "auto"},
                           books=["sportsbet"])
    assert live.scope_for(policy) == ["sportsbet.write"]


def test_every_known_book_has_a_reader() -> None:
    """A book the plane can place at but cannot re-price would have its drift gate
    silently skipped."""
    for book in BettingPolicy.KNOWN_BOOKS:
        assert live.reader_for(book) is not None


def test_an_unknown_book_has_no_reader() -> None:
    with pytest.raises(PriceUnreadable, match="no re-price reader"):
        live.reader_for("someothercorp")
