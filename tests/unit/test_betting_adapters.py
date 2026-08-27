"""Building each book's placement body — four unrelated shapes, one wrong turn each.

Every assertion here maps to something captured from a real placement on 2026-08-27, not
to a shape anyone deduced. The comments name the trap each one guards.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.betting import adapters
from sportsdata_agents.betting.adapters import AdapterError
from sportsdata_agents.betting.scanner import Candidate

pytestmark = pytest.mark.unit


def candidate(book: str, placement: dict | None, odds: float = 3.4) -> Candidate:
    return Candidate(
        book=book, fixture_id="f1",
        legs=[{"market": "h2h"}, {"market": "total"}],
        odds=odds, consensus_odds=3.0, edge=0.13,
        edge_basis="relative", books_in_consensus=2,
        quote={"placement": placement} if placement is not None else {},
    )


SPORTSBET = {
    "classExternalId": 103, "competitionExternalId": 17131, "eventExternalId": 9876,
    "parts": [{"marketExternalId": 1, "outcomeExternalId": 11},
              {"marketExternalId": 2, "outcomeExternalId": 22}],
    "priceNum": 12, "priceDen": 5,
}
ENTAIN = {
    "event_id": "e-1",
    "selections": [{"market_id": "m1", "entrant_id": "x1"},
                   {"market_id": "m2", "entrant_id": "x2"}],
    "odds": {"numerator": 12, "denominator": 5, "decimal": 3.4},
}
UNIBET = {"event_id": 1028856020,
          "outcome_ids": [4306981997, 4309036845], "odds_thousandths": 3300}


# ─── Sportsbet ──────────────────────────────────────────────────────────


def test_a_sportsbet_sgm_is_one_leg_with_several_parts() -> None:
    """THE trap. An SGM is not a multi-leg bet at Sportsbet — it is betType "SGL" with
    one leg carrying several `parts`. Building it as several legs is a different bet."""
    body = adapters.sportsbet_payload(candidate("sportsbet", SPORTSBET), stake=2.5)
    item = body["betItems"][0]
    assert item["betType"] == "SGL"
    assert len(item["legs"]) == 1
    assert len(item["legs"][0]["parts"]) == 2


def test_the_combined_price_rides_on_every_part() -> None:
    parts = adapters.sportsbet_payload(candidate("sportsbet", SPORTSBET), stake=1)["betItems"][0]["legs"][0]["parts"]
    assert all(p["priceNum"] == 12 and p["priceDen"] == 5 for p in parts)


def test_sportsbet_uses_the_external_id_space() -> None:
    """103/17131, not the internal 50/4165 that topicLink carries — the internal pair
    returns HTTP 500 from the pricer. The quoter resolved this; the adapter must not
    re-derive it."""
    leg = adapters.sportsbet_payload(candidate("sportsbet", SPORTSBET), stake=1)["betItems"][0]["legs"][0]
    assert leg["classExternalId"] == 103
    assert leg["competitionExternalId"] == 17131


def test_full_details_is_requested_so_there_is_a_receipt() -> None:
    """Without it there is no betId, and betId is the only thing a later
    sportsbet_bet_history read can confirm the placement against."""
    assert adapters.sportsbet_payload(candidate("sportsbet", SPORTSBET), stake=1)["fullDetails"] is True


def test_the_stake_is_the_callers_and_appears_once() -> None:
    body = adapters.sportsbet_payload(candidate("sportsbet", SPORTSBET), stake=7.5)
    assert body["betItems"][0]["stakePerLine"] == 7.5
    assert body["betItems"][0]["numLines"] == 1


# ─── Entain ─────────────────────────────────────────────────────────────


def test_an_entain_sgm_is_several_legs_in_one_bet() -> None:
    """The mirror image of Sportsbet's trap, and the reason both adapters exist."""
    body = adapters.entain_payload(candidate("entain", ENTAIN), stake=2)
    assert len(body["bets"]) == 1
    assert len(body["bets"][0]["legs"]) == 2


def test_entain_carries_a_prices_object_keyed_by_event_id() -> None:
    body = adapters.entain_payload(candidate("entain", ENTAIN), stake=2)
    assert body["bets"][0]["prices"] == {"e-1": {"valid": True, "odds": ENTAIN["odds"]}}


def test_entain_stake_is_top_level_not_per_bet() -> None:
    assert adapters.entain_payload(candidate("entain", ENTAIN), stake=2)["stake"] == 2


# ─── Unibet / Kambi ─────────────────────────────────────────────────────


def test_a_kambi_sgm_is_one_couponrow_nesting_the_legs() -> None:
    """operation "AND", type "BET_BUILDER" — both read off a live request. The first
    version guessed "COMBINATION" for both, which Kambi would not have recognised."""
    body = adapters.unibet_payload(candidate("unibet", UNIBET), stake=1)["body"]
    rows = body["couponRows"]
    assert len(rows) == 1
    assert rows[0]["type"] == "BET_BUILDER"
    assert rows[0]["group"]["operation"] == "AND"
    assert all(g["operation"] == "AND" for g in rows[0]["group"]["groups"])
    assert [g["outcomeIds"] for g in rows[0]["group"]["groups"]] == [[4306981997], [4309036845]]


def test_kambi_odds_are_sent_in_thousandths() -> None:
    """3300 IS 3.30. Sending the decimal would be a price a thousand times too short."""
    body = adapters.unibet_payload(candidate("unibet", UNIBET), stake=1)["body"]
    assert body["couponRows"][0]["odds"] == 3300


def test_the_kambi_stake_lives_in_bets_not_on_the_row() -> None:
    body = adapters.unibet_payload(candidate("unibet", UNIBET), stake=4.5)["body"]
    assert body["bets"] == [{"couponRowIndexes": [0], "eachWay": False, "stake": 4.5}]


def test_the_guessed_fields_are_not_sent() -> None:
    """allowOddsChange / requestId / channel were invented and do not appear on the
    verified request. Drift is handled by this plane's gate before the body is built."""
    body = adapters.unibet_payload(candidate("unibet", UNIBET), stake=1)["body"]
    for guessed in ("allowOddsChange", "allowOddsChangeLive", "allowOddsChangePreMatch",
                    "requestId", "channel"):
        assert guessed not in body, guessed
    assert body["isUserLoggedIn"] is True


def test_the_stake_is_what_separates_asking_from_betting() -> None:
    """validate carries no stake; placement adds it. Same coupon otherwise."""
    ask = adapters.unibet_coupon(candidate("unibet", UNIBET), stake=None)["body"]
    bet = adapters.unibet_coupon(candidate("unibet", UNIBET), stake=2.5)["body"]
    assert "stake" not in ask["bets"][0]
    assert bet["bets"][0]["stake"] == 2.5
    assert ask["couponRows"] == bet["couponRows"]


def test_unibet_re_prices_through_the_pricer_not_the_validator() -> None:
    """unibet_sgm_price takes an eventId and comma-joined outcome ids — not a coupon."""
    args = adapters.reprice_args_for(candidate("unibet", UNIBET), stake=1)
    assert args["eventId"] == 1028856020
    assert args["outcomeIds"] == "4306981997,4309036845"
    assert "body" not in args


# ─── TAB: the one that cannot come from a quote ─────────────────────────


SLIP = {"bets": [{"legs": [
    {"decoToken": "tok-a", "type": "WIN", "propositionId": 1016, "odds": "2.00"},
    {"decoToken": "tok-b", "type": "WIN", "propositionId": 2048, "odds": "1.70"},
]}]}


def test_tab_cannot_be_built_from_a_comparison_quote() -> None:
    """Only the account-tier tab_price_slip issues decoTokens; the anonymous pricer used
    for comparison does not. The error has to say so rather than failing obscurely."""
    with pytest.raises(AdapterError, match="tab_price_slip"):
        adapters.payload_for(candidate("tab", None), stake=1)


def test_tab_is_built_from_the_slip_response() -> None:
    body = adapters.tab_payload(SLIP, stake=1.5, account_number="123")
    assert body["decoTokens"] == ["tok-a", "tok-b"]
    assert [leg["decoToken"] for leg in body["bets"][0]["legs"]] == ["tok-a", "tok-b"]


def test_tab_stake_and_odds_are_strings() -> None:
    """TAB's wire format. Numbers are rejected."""
    body = adapters.tab_payload(SLIP, stake=1.5, account_number="123")
    assert body["bets"][0]["stake"] == "1.50"
    assert all(isinstance(leg["odds"], str) for leg in body["bets"][0]["legs"])


def test_the_transaction_id_is_returned_so_it_can_be_stored_first() -> None:
    """It is TAB's idempotency key and the only real one of the four books — resending
    with the SAME id asks whether the bet landed; a fresh id places a second bet. The
    caller must be able to persist it BEFORE the request goes out."""
    body = adapters.tab_payload(SLIP, stake=1, account_number="123", transaction_id="fixed-1")
    assert body["transactionId"] == "fixed-1"
    auto = adapters.tab_payload(SLIP, stake=1, account_number="123")
    assert auto["transactionId"] and auto["transactionId"] != "fixed-1"


def test_a_leg_without_a_token_is_refused() -> None:
    """A token from an older enquiry is a different quote, and a missing one TAB will
    simply not accept."""
    bad = {"bets": [{"legs": [{"decoToken": None, "type": "WIN", "propositionId": 1, "odds": "2.0"}]}]}
    with pytest.raises(AdapterError, match="decoToken"):
        adapters.tab_payload(bad, stake=1, account_number="123")


# ─── the shared failure ─────────────────────────────────────────────────


@pytest.mark.parametrize("book", ["sportsbet", "entain", "unibet"])
def test_a_quote_without_resolution_cannot_be_placed(book: str) -> None:
    """A quote from a path that never resolved the book's own ids is not placeable, and
    saying so plainly beats a KeyError halfway down the money path."""
    with pytest.raises(AdapterError, match="placement"):
        adapters.payload_for(candidate(book, None), stake=1)


def test_an_unknown_book_has_no_adapter() -> None:
    with pytest.raises(AdapterError, match="no placement adapter"):
        adapters.payload_for(candidate("someothercorp", {"x": 1}), stake=1)
