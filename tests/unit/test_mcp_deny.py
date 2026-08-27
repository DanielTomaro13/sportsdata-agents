"""The money-tool CLASSIFIER (pure logic, no subprocess).

This file used to pin a no-money deny-filter: any tool whose name matched money verbs
was hidden from agents and refused on call. That ban was removed on 2026-08-27, when the
data plane gained real placement tools and the premise it rested on ("the MCP has no
placement tools at source") stopped being true. See `mcp/manager.py`.

What is tested now is the replacement: a narrower classifier that LABELS money-movers so
they can be logged and asserted about, while the actual decision to place a bet belongs
to `sportsdata_agents.betting.policy` — which reasons about edge, stake and budget
rather than about spelling.

The distinction the old filter could not draw, and this one must: **writes move money,
reads do not.** `sportsbet_place_bet` moves money. `sportsbet_price_slip`,
`betfair_cashout` and an account balance do not — and the old filter hid all three,
which cost Kelly sizing its bankroll and cost the executor its pre-placement quote.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.mcp.manager import ForbiddenToolError, is_denied, moves_money

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    [
        "sportsbet_place_bet",
        "tab_place_bet",
        "entain_place_bet",
        "unibet_place_bet",
        "sportsbook_placeBet",
        "tab_deposit_funds",
        "account_withdraw",
        "wallet_transfer",
    ],
)
def test_real_money_movers_are_labelled(name: str) -> None:
    assert moves_money(name)


@pytest.mark.parametrize(
    "name",
    [
        # Ordinary data tools.
        "mlb_teams",
        "mlb_player",           # contains "play", not "place_bet"
        "openf1_sessions",
        "sportsbet_racecard",
        "pinnacle_matchup_markets",
        "list_tools_by_capability",
        # READS the old filter wrongly hid. Each is now available again, and each has a
        # concrete use in the betting plane.
        "sportsbet_price_slip",     # the quote you must take immediately before placing
        "tab_price_slip",
        "unibet_validate_coupon",   # the anonymous pre-placement go/no-go
        "betfair_cashout",          # read-only availability feed
        "get_balance",              # Kelly sizing needs a bankroll from somewhere
        "sportsbet_bet_history",    # how a placement is CONFIRMED
    ],
)
def test_reads_and_data_tools_are_not_money_movers(name: str) -> None:
    assert not moves_money(name)


def test_in_game_currency_is_not_real_money() -> None:
    """FPL transfers move a footballer between a squad and a bench. The classifier is a
    name matcher and cannot tell the two senses of "transfer" apart, so these are
    exempted by exact name."""
    assert not moves_money("fpl_transfers")
    assert not moves_money("fpl_propose_transfer")


def test_nothing_is_denied_by_name_any_more() -> None:
    """The ban is gone, and its absence is asserted rather than left implicit — a future
    reader finding `is_denied` should not assume it still gates anything."""
    for name in ("sportsbet_place_bet", "account_withdraw", "get_balance", "mlb_teams"):
        assert is_denied(name) is False


def test_forbidden_error_still_names_the_tool() -> None:
    """No longer raised by the manager, but the betting plane's own refusals are the
    right place for it, so the shape is kept."""
    err = ForbiddenToolError("sportsbet_place_bet")
    assert err.tool == "sportsbet_place_bet"
    assert "refused by policy" in str(err)
