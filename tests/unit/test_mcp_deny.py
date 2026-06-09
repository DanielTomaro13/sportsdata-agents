"""M0.4 — the no-money deny-filter (pure logic, no subprocess)."""

from __future__ import annotations

import pytest

from sportsdata_agents.mcp.manager import ForbiddenToolError, is_denied

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    [
        "place_bet",
        "sportsbook_placeBet",
        "tab_deposit_funds",
        "account_withdraw",
        "set_stake",
        "wallet_transfer",
        "betslip_submit",
        "betfair_cashout",  # read-only, but strictly hidden — accepted cost
        "checkout",
        "get_balance",
    ],
)
def test_money_ish_names_are_denied(name: str) -> None:
    assert is_denied(name)


@pytest.mark.parametrize(
    "name",
    [
        "mlb_teams",
        "mlb_player",  # contains "play", not "place"
        "openf1_sessions",
        "datagolf_outrights",
        "sportsbet_racecard",
        "tab_sports",
        "betfair_market_prices",
        "list_tools_by_capability",
        "cricketaustralia_scorecard",
        "pinnacle_matchup_markets",
    ],
)
def test_data_tools_pass(name: str) -> None:
    assert not is_denied(name)


def test_forbidden_error_names_the_tool() -> None:
    err = ForbiddenToolError("place_bet")
    assert err.tool == "place_bet"
    assert "no-money" in str(err)
