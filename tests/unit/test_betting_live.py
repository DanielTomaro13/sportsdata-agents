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
    """3300 IS 3.30. Returning the raw number is a price a thousand times too long."""
    quote = {"selectedOdds": {"decimal": 3300}, "selectedOutcomeIds": [1, 2]}
    assert live.read_unibet_price(quote) == pytest.approx(3.3)


def test_unibet_re_prices_through_the_pricer_not_the_validator() -> None:
    """validate_coupon answers {status, validSession, rewardInfo} and echoes NO price.
    This reader used to parse couponRows out of it, found nothing, raised — and the
    executor refused every Unibet placement. A drift gate armed and unable to pass."""
    validate_reply = {"status": "SUCCESS", "validSession": True, "rewardInfo": {}}
    with pytest.raises(PriceUnreadable):
        live.read_unibet_price(validate_reply)


def test_a_single_leg_has_no_price_and_says_so() -> None:
    """unibet_sgm_price returns the combinable list with no selectedOdds for one leg —
    a missing key means "not priced", never zero."""
    with pytest.raises(PriceUnreadable, match="single leg"):
        live.read_unibet_price({"combinableOutcomeIds": [1, 2, 3]})


def test_kambi_with_no_rows_refuses() -> None:
    with pytest.raises(PriceUnreadable):
        live.read_unibet_price({"couponRows": []})


# ─── scoping ────────────────────────────────────────────────────────────


def test_pricing_groups_are_a_table_because_the_names_disagree() -> None:
    """Read from the shipped specs, not derived. `sportsbet.sports` and `tab.sports` are
    plural, `unibet.sport` and `betr.sport` singular, and Entain's pricing is in
    `entain.rest` with no "sport" in it. Guessing f"{book}.sport" was wrong for three of
    four books, and the symptom was a scan that priced nothing rather than an error."""
    assert live.PRICING_GROUPS["sportsbet"] == ["sportsbet.sports", "sportsbet.cross"]
    assert live.PRICING_GROUPS["unibet"] == ["unibet.sport"]
    assert live.PRICING_GROUPS["entain"] == ["entain.rest"]
    assert live.PRICING_GROUPS["tab"] == ["tab.sports"]


def test_one_book_can_need_several_pricing_groups() -> None:
    """Sportsbet resolves markets from `sportsbet.sports` but its SGM PRICER lives in
    `sportsbet.cross` — a session with only the first resolves legs it then cannot
    price. This is why the mapping is a list per book."""
    groups = live.read_groups(["sportsbet"])
    assert "sportsbet.sports" in groups and "sportsbet.cross" in groups


def test_read_groups_covers_every_comparable_book_not_just_placeable_ones() -> None:
    """A book you cannot bet at still informs the consensus; dropping it narrows the
    field the edge is measured against."""
    groups = live.read_groups()
    assert live.PRICING_GROUPS["betr"][0] in groups
    assert live.PRICING_GROUPS["pointsbet"][0] in groups


def test_a_placing_session_can_also_re_price() -> None:
    """Sportsbet's and TAB's price_slip are ACCOUNT-tier tools, so a session holding only
    `<book>.write` can place a bet but not check the price first — arming the drift gate
    and then starving it."""
    policy = BettingPolicy(book_modes={"sportsbet": "auto"}, books=["sportsbet"])
    assert live.scope_for(policy) == ["sportsbet.account", "sportsbet.write"]

    tab = BettingPolicy(book_modes={"tab": "ask"}, books=["tab"])
    assert live.scope_for(tab) == ["tab.account", "tab.write"]


def test_a_paper_policy_can_place_nowhere() -> None:
    """The default. No write group means the placement tools are ABSENT from the
    session, not merely declined by the policy."""
    assert live.scope_for(BettingPolicy()) == []


def test_only_books_set_to_ask_or_auto_get_a_write_group() -> None:
    policy = BettingPolicy(book_modes={"sportsbet": "auto", "tab": "ask",
                                       "unibet": "paper", "entain": "never"})
    groups = live.scope_for(policy)
    assert "sportsbet.write" in groups and "tab.write" in groups
    assert not any(g.startswith(("unibet", "entain")) for g in groups)


def test_a_book_outside_the_eligible_list_gets_no_write_group() -> None:
    policy = BettingPolicy(book_modes={"sportsbet": "auto", "tab": "auto"},
                           books=["sportsbet"])
    assert live.scope_for(policy) == ["sportsbet.account", "sportsbet.write"]


def test_every_known_book_has_a_reader() -> None:
    """A book the plane can place at but cannot re-price would have its drift gate
    silently skipped."""
    for book in BettingPolicy.KNOWN_BOOKS:
        assert live.reader_for(book) is not None


def test_an_unknown_book_has_no_reader() -> None:
    with pytest.raises(PriceUnreadable, match="no re-price reader"):
        live.reader_for("someothercorp")


# ─── the three tables that must agree ───────────────────────────────────


def test_every_book_that_re_prices_has_args_a_tool_and_a_reader() -> None:
    """THE cross-file guard. Retargeting Unibet's re-price from validate_coupon to the
    anonymous pricer touched the args builder and the reader but MISSED the tool table
    in execute.py, which put back the "refuses every Unibet placement" bug under a commit
    message claiming it fixed. Three places have to agree; this asserts they do."""
    from sportsdata_agents.betting import adapters
    from sportsdata_agents.betting.execute import REPRICE_TOOL

    for book in adapters.REPRICE_ARGS:
        assert book in REPRICE_TOOL, f"{book} builds re-price args but has no tool"
        assert book in live.READERS, f"{book} builds re-price args but has no reader"


def test_the_reprice_tool_table_names_the_pricer_for_unibet() -> None:
    """validate_coupon echoes no price at all, so pointing the drift gate at it makes
    every placement refuse. Named explicitly because the table looks plausible either way."""
    from sportsdata_agents.betting.execute import REPRICE_TOOL

    assert REPRICE_TOOL["unibet"] == "unibet_sgm_price"


def test_kambis_payout_ceiling_is_refused_as_a_price() -> None:
    """1001.0 is where a long Kambi multi stops moving while the true price keeps
    drifting behind it. The drift gate is one-sided, so a capped value reads as "held or
    improved" and would place a bet at a price nobody knows."""
    with pytest.raises(PriceUnreadable, match="ceiling"):
        live.read_unibet_price({"selectedOdds": {"decimal": 1_001_000}})
    # and the ordinary case still reads
    assert live.read_unibet_price({"selectedOdds": {"decimal": 3300}}) == pytest.approx(3.3)
