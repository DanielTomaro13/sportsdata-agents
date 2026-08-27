"""The cross-book same-game-multi comparator.

One combination, priced by each book's own correlation model, on comparable units. No
single book's app shows that, and the books disagree by far more than the vig — measured
live on 2026-08-27, correlation adjustments on one anchor leg ran from -41% (Unibet) to
+5% (Entain), so the spread between books on the same legs is a real number.

Two things have to be right or the comparison is worse than useless:

  * THE UNITS. Nothing in any payload announces them and the books do not agree —
    Sportsbet and Entain send fractions, Unibet sends thousandths, the rest send decimals.
    Compare a raw 3400 against a raw 3.60 and one book appears to pay a thousand times
    better than another.
  * A ZERO IS A REFUSAL. PointsBet and BetR answer a refusal with HTTP 200 and a price of
    0. Ranked by price a zero sorts last, which is survivable — but treated as a quote it
    is a bookmaker's name against a bet that does not exist.

The leg matchers are tested against REAL captured payloads, because the shapes they pick
through are the part that drifts.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.interfaces.sportsboard.sgm_books import (
    _QUOTERS,
    BOOKMAKERS,
    PRICE_UNITS,
    _decimal_from,
    _match_pointsbet_leg,
    _match_unibet_leg,
)

# ─── units ──────────────────────────────────────────────────────────────


def test_every_bookmaker_has_declared_units():
    """A book in the selector with no entry here would be compared in whatever units it
    happens to send."""
    assert set(PRICE_UNITS) >= set(BOOKMAKERS)


@pytest.mark.parametrize("book,raw,want", [
    # fractional: decimal = 1 + n/d. 27/10 is 3.70 — NOT 2.7, which is the quiet error.
    ("entain", {"numerator": 27, "denominator": 10}, 3.70),
    ("sportsbet", {"numerator": 102, "denominator": 100}, 2.02),
    # thousandths: Kambi scales by 1000. 3400 is 3.40, not 3400.
    ("unibet", 3400, 3.40),
    ("unibet", 1920, 1.92),
    # already decimal
    ("pointsbet", 3.6, 3.6),
    ("betr", 2.2, 2.2),
])
def test_prices_normalise_to_decimal(book, raw, want):
    assert _decimal_from(book, raw) == pytest.approx(want)


def test_a_zero_price_is_not_a_quote():
    """PointsBet and BetR both answer a refusal with HTTP 200 and price 0. The engine
    raises those, but a zero reaching here anyway must not become a number."""
    assert _decimal_from("pointsbet", 0) is None
    assert _decimal_from("betr", 0) is None
    assert _decimal_from("betr", 0.0) is None


def test_a_price_below_evens_is_not_a_quote():
    """Decimal odds are strictly greater than 1. A 0.9 is a scale error or a refusal, and
    either way it is not something to put a bookmaker's name on."""
    assert _decimal_from("pointsbet", 0.9) is None
    assert _decimal_from("unibet", 900) is None      # 0.9 after scaling


def test_a_shape_we_do_not_recognise_returns_nothing():
    """Strictness on purpose: a guess here is a wrong price attributed to a real book."""
    assert _decimal_from("entain", 3.4) is None               # fractional book, decimal sent
    assert _decimal_from("entain", {"numerator": 1}) is None  # no denominator
    assert _decimal_from("entain", {"numerator": 1, "denominator": 0}) is None
    assert _decimal_from("pointsbet", None) is None
    assert _decimal_from("pointsbet", "not a price") is None
    assert _decimal_from("nosuchbook", 3.4) is None


def test_the_two_scale_errors_would_be_caught():
    """The whole reason the table exists, stated as the two failures it prevents."""
    assert _decimal_from("unibet", 3400) != 3400              # 1000x too large — loud
    assert _decimal_from("entain", {"numerator": 27, "denominator": 10}) != 2.7  # quiet


# ─── the dispatcher ─────────────────────────────────────────────────────


def test_no_book_can_fall_through_to_another_books_quoter():
    """The bug this prevents: a book whose resolver was never written quietly returning
    the price from whichever quoter the dispatcher fell through to — one book's number
    under another book's name."""
    for book in _QUOTERS:
        assert book in BOOKMAKERS


# ─── leg matchers, against real captured payloads ───────────────────────

HOME, AWAY = "Western Bulldogs", "Collingwood"

#: Trimmed from a live PointsBet event (114096420 = Match Result, 114104204 = Line,
#: 114104206 = Total Points). The two ids that matter are BOTH "11".
POINTSBET_MARKETS = [
    {"key": "114096420", "eventClass": "Match Result", "outcomes": [
        {"key": "1", "name": "Western Bulldogs", "price": 1.96, "isOpenForBetting": True},
        {"key": "2", "name": "Collingwood", "price": 1.84, "isOpenForBetting": True}]},
    {"key": "114104204", "eventClass": "Line", "outcomes": [
        {"key": "11", "name": "Western Bulldogs +1.5", "price": 1.9, "isOpenForBetting": True},
        {"key": "12", "name": "Collingwood -1.5", "price": 1.9, "isOpenForBetting": True}]},
    {"key": "114104206", "eventClass": "Total Points Over/Under", "outcomes": [
        {"key": "11", "name": "Over 169.5", "price": 1.9, "isOpenForBetting": True},
        {"key": "12", "name": "Under 169.5", "price": 1.9, "isOpenForBetting": True}]},
]


def test_pointsbet_returns_the_market_and_outcome_together():
    """OutcomeKey "11" is the Bulldogs line in one market and Over 169.5 in another. The
    pair is the identity, so a matcher that returned an outcome key alone would price a
    different bet and still return 200."""
    line = _match_pointsbet_leg({"market": "line", "selection": "home", "line": 1.5},
                                POINTSBET_MARKETS, HOME, AWAY)
    total = _match_pointsbet_leg({"market": "total", "selection": "over", "line": 169.5},
                                 POINTSBET_MARKETS, HOME, AWAY)
    assert line == {"MarketKey": "114104204", "OutcomeKey": "11"}
    assert total == {"MarketKey": "114104206", "OutcomeKey": "11"}
    assert line["OutcomeKey"] == total["OutcomeKey"], "the collision this guards is real"
    assert line["MarketKey"] != total["MarketKey"]


def test_pointsbet_matches_head_to_head_by_team():
    assert _match_pointsbet_leg({"market": "h2h", "selection": "home"},
                                POINTSBET_MARKETS, HOME, AWAY)["OutcomeKey"] == "1"
    assert _match_pointsbet_leg({"market": "h2h", "selection": "away"},
                                POINTSBET_MARKETS, HOME, AWAY)["OutcomeKey"] == "2"


def test_pointsbet_reports_a_miss_as_a_reason_not_a_guess():
    for leg in ({"market": "h2h", "selection": "draw"},
                {"market": "total", "selection": "over", "line": 999.5},
                {"market": "first_scorer", "selection": "home"},
                {"market": "total", "selection": "over"}):            # no line
        assert isinstance(_match_pointsbet_leg(leg, POINTSBET_MARKETS, HOME, AWAY), str)


def test_pointsbet_skips_a_suspended_outcome():
    shut = [{"key": "1", "eventClass": "Match Result", "outcomes": [
        {"key": "1", "name": "Western Bulldogs", "price": 1.96, "isOpenForBetting": False}]}]
    assert isinstance(_match_pointsbet_leg({"market": "h2h", "selection": "home"},
                                           shut, HOME, AWAY), str)


#: Trimmed from a live Kambi event. Note lines are scaled by 1000 and the fixture carries
#: BOTH a match total and a team sub-total sitting on the same line.
UNIBET_OFFERS = [
    {"id": 1, "betOfferType": {"name": "Head to Head"},
     "criterion": {"label": "Including Overtime"}, "outcomes": [
        {"id": 4306981996, "label": "1", "participant": "Western Bulldogs", "odds": 1930},
        {"id": 4306981997, "label": "2", "participant": "Collingwood", "odds": 1840}]},
    {"id": 2, "betOfferType": {"name": "Totals"},
     "criterion": {"label": "Total Points by Western Bulldogs - Including Overtime"},
     "outcomes": [{"id": 999001, "label": "Over", "line": 170500, "odds": 1900},
                  {"id": 999002, "label": "Under", "line": 170500, "odds": 1900}]},
    {"id": 3, "betOfferType": {"name": "Totals"},
     "criterion": {"label": "Total Points - Including Overtime"},
     "outcomes": [{"id": 4309057043, "label": "Over", "line": 170500, "odds": 1880},
                  {"id": 4309057044, "label": "Under", "line": 170500, "odds": 1880}]},
    {"id": 4, "betOfferType": {"name": "Line"},
     "criterion": {"label": "Line - Including Overtime"}, "outcomes": [
        {"id": 4306985023, "label": "Western Bulldogs", "participant": "Western Bulldogs",
         "line": 1500, "odds": 1880}]},
]


def test_unibet_prefers_the_match_total_over_a_team_sub_total():
    """Kambi ships 56 Totals offers on one AFL fixture; most are per-team or per-period.
    A team total on the same line would otherwise shadow the match total and quote a
    different bet under the same label."""
    got = _match_unibet_leg({"market": "total", "selection": "over", "line": 170.5},
                            UNIBET_OFFERS, HOME, AWAY)
    assert got == 4309057043, "matched the team sub-total instead of the match total"


def test_unibet_scales_the_line_by_a_thousand():
    """169.5 arrives as 169500. The board's line is scaled UP rather than Kambi's scaled
    down, because integers compare exactly."""
    assert _match_unibet_leg({"market": "total", "selection": "under", "line": 170.5},
                             UNIBET_OFFERS, HOME, AWAY) == 4309057044
    assert isinstance(_match_unibet_leg({"market": "total", "selection": "over", "line": 170.6},
                                        UNIBET_OFFERS, HOME, AWAY), str)


def test_unibet_matches_head_to_head_and_line_by_participant():
    assert _match_unibet_leg({"market": "h2h", "selection": "home"},
                             UNIBET_OFFERS, HOME, AWAY) == 4306981996
    assert _match_unibet_leg({"market": "line", "selection": "home", "line": 1.5},
                             UNIBET_OFFERS, HOME, AWAY) == 4306985023


def test_unibet_skips_an_outcome_with_no_price():
    """A suspended Kambi outcome carries no `odds` key at all, rather than a zero."""
    suspended = [{"id": 1, "betOfferType": {"name": "Head to Head"},
                  "criterion": {"label": "x"},
                  "outcomes": [{"id": 7, "label": "1", "participant": "Western Bulldogs"}]}]
    assert isinstance(_match_unibet_leg({"market": "h2h", "selection": "home"},
                                        suspended, HOME, AWAY), str)


# ─── the comparison itself ──────────────────────────────────────────────


class _StubSession:
    """compare() only threads the session down to the quoters, which are stubbed here."""


async def _compare_with(monkeypatch, quotes: dict, legs=None):
    from sportsdata_agents.interfaces.sportsboard import sgm_books as m

    async def fake_quote(session, mcp, book, fixture_id, legs_):
        r = quotes[book]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(m, "quote", fake_quote)
    return await m.compare(_StubSession(), object(), "fix-1",
                           legs or [{"market": "h2h", "selection": "home"},
                                    {"market": "total", "selection": "over", "line": 169.5}],
                           books=tuple(quotes))


async def test_the_best_price_wins_and_the_spread_is_reported(monkeypatch):
    out = await _compare_with(monkeypatch, {
        "sportsbet": {"book_odds": 3.60},
        "pointsbet": {"book_odds": 3.40},
        "unibet": {"book_odds": 3.75},
    })
    assert [q["book"] for q in out["quotes"]] == ["unibet", "sportsbet", "pointsbet"]
    assert out["best"] == {"book": "unibet", "book_odds": 3.75}
    # 3.75 vs 3.40 on the same legs — 10.3% more for an identical bet.
    assert out["spread_pct"] == pytest.approx(10.29, abs=0.01)
    assert out["books_priced"] == 3


async def test_the_legs_are_restated_with_the_answer(monkeypatch):
    """Four of the seven pricers can return a price for a different bet than the one
    asked for. The legs travel with the quote so a caller never has to assume."""
    legs = [{"market": "h2h", "selection": "home"}, {"market": "total", "selection": "over", "line": 169.5}]
    out = await _compare_with(monkeypatch, {"sportsbet": {"book_odds": 3.6}}, legs=legs)
    assert out["legs"] == legs


async def test_one_book_failing_does_not_stop_the_others(monkeypatch):
    """A comparator that stopped at the first refusal would show one price and call it
    the market. Books are quoted concurrently and independently."""
    out = await _compare_with(monkeypatch, {
        "sportsbet": {"book_odds": 3.60},
        "pointsbet": {"unavailable": "no pointsbet event linked to this fixture"},
        "unibet": RuntimeError("connection reset"),
    })
    assert out["books_priced"] == 1
    assert out["best"]["book"] == "sportsbet"
    assert "no pointsbet event" in out["unavailable"]["pointsbet"]
    # A resolver blowing up is reported, not swallowed and not fatal.
    assert "RuntimeError" in out["unavailable"]["unibet"]


async def test_no_book_pricing_says_so_rather_than_returning_empty(monkeypatch):
    out = await _compare_with(monkeypatch, {
        "sportsbet": {"unavailable": "refused"},
        "pointsbet": {"unavailable": "refused"},
    })
    assert out["quotes"] == []
    assert out["books_priced"] == 0
    assert "no book would price" in out["note"]
    assert set(out["unavailable"]) == {"sportsbet", "pointsbet"}


async def test_a_single_book_reports_no_spread(monkeypatch):
    """One quote is not a comparison, and a spread against itself would read as 0% —
    which looks like agreement between books rather than absence of them."""
    out = await _compare_with(monkeypatch, {"sportsbet": {"book_odds": 3.6}})
    assert out["best"]["book_odds"] == 3.6
    assert "spread_pct" not in out


# ─── BetR ───────────────────────────────────────────────────────────────

#: BetR calls a MARKET GROUP an "Event": 91686300 is Match Result, 91712212 is Total
#: Points, both inside one MasterEvent. OutcomeId restarts per group.
BETR_EVENTS = [
    {"EventId": 91686300, "EventName": "Match Result", "Outcomes": [
        {"OutcomeId": 1, "OutcomeName": "Western Bulldogs", "MarketTypeCode": "WIN",
         "GroupByHeader": "Match Result", "Points": 0.0, "Price": 1.95, "IsOpenForBetting": True},
        {"OutcomeId": 2, "OutcomeName": "Collingwood", "MarketTypeCode": "WIN",
         "GroupByHeader": "Match Result", "Points": 0.0, "Price": 1.85, "IsOpenForBetting": True},
        {"OutcomeId": 1, "OutcomeName": "Western Bulldogs", "MarketTypeCode": "HCWEST",
         "GroupByHeader": "Handicap", "Points": 1.5, "Price": 1.9, "IsOpenForBetting": True}]},
    {"EventId": 91712212, "EventName": "Total Points", "Outcomes": [
        {"OutcomeId": 13910, "OutcomeName": "Over 139.5", "MarketTypeCode": "WIN",
         "GroupByHeader": "Total Points", "Points": 0.0, "Price": 1.1, "IsOpenForBetting": True}]},
]


def test_betr_carries_the_market_type_because_it_fails_silently_without_it():
    """Dropping MarketType turned a verified 2.20 into 21 with ErrorNo 0 — a wrong answer,
    not an error. It is copied off the outcome rather than inferred."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_betr_leg
    hit = _match_betr_leg({"market": "h2h", "selection": "home"}, BETR_EVENTS, HOME, AWAY)
    assert hit == {"EventId": 91686300, "OutcomeId": 1, "MarketType": "WIN"}


def test_betr_outcome_ids_repeat_across_groups_so_the_event_travels_too():
    """OutcomeId 1 is the Bulldogs head-to-head in one group and the Bulldogs handicap in
    another. Same collision as PointsBet, different vocabulary."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_betr_leg
    h2h = _match_betr_leg({"market": "h2h", "selection": "home"}, BETR_EVENTS, HOME, AWAY)
    line = _match_betr_leg({"market": "line", "selection": "home", "line": 1.5},
                           BETR_EVENTS, HOME, AWAY)
    assert h2h["OutcomeId"] == line["OutcomeId"] == 1
    assert h2h["MarketType"] != line["MarketType"]


def test_betr_reads_the_handicap_off_points_not_the_name():
    """The outcome name is just the team; the line lives on `Points`."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_betr_leg
    assert isinstance(_match_betr_leg({"market": "line", "selection": "home", "line": 9.5},
                                      BETR_EVENTS, HOME, AWAY), str)


def test_betr_skips_a_suspended_or_unpriced_outcome():
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_betr_leg
    shut = [{"EventId": 1, "Outcomes": [
        {"OutcomeId": 1, "OutcomeName": "Western Bulldogs", "MarketTypeCode": "WIN",
         "GroupByHeader": "Match Result", "Price": None, "IsOpenForBetting": True}]}]
    assert isinstance(_match_betr_leg({"market": "h2h", "selection": "home"},
                                      shut, HOME, AWAY), str)


# ─── Entain ─────────────────────────────────────────────────────────────

ENTAIN_MARKETS = {
    "mb": {"id": "mb", "name": "Match Betting", "handicap": None,
           "same_game_multi_available": True, "visible": True},
    "ln": {"id": "ln", "name": "Line", "handicap": -5.5,
           "same_game_multi_available": True, "visible": True},
    "tp": {"id": "tp", "name": "Total Points", "handicap": 173.5,
           "same_game_multi_available": True, "visible": True},
    "hs": {"id": "hs", "name": "Highest Scoring Half", "handicap": None,
           "same_game_multi_available": None, "visible": True},   # flagged NOT sgm
}
ENTAIN_ENTRANTS = {
    "mb-h": {"id": "mb-h", "name": "Melbourne", "home_away": "HOME", "market_id": "mb", "visible": True},
    "mb-a": {"id": "mb-a", "name": "Carlton", "home_away": "AWAY", "market_id": "mb", "visible": True},
    "ln-h": {"id": "ln-h", "name": "Melbourne", "home_away": "HOME", "market_id": "ln", "visible": True},
    "tp-o": {"id": "tp-o", "name": "Over", "market_id": "tp", "visible": True},
    "tp-u": {"id": "tp-u", "name": "Under", "market_id": "tp", "visible": True},
    "hs-1": {"id": "hs-1", "name": "1st Half", "market_id": "hs", "visible": True},
}


def test_entain_picks_the_side_by_home_away_not_by_team_name():
    """The one identifier in Entain's payload that cannot drift with a naming change."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_entain_leg
    assert _match_entain_leg({"market": "h2h", "selection": "home"},
                             ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton") == \
        {"market_id": "mb", "entrant_id": "mb-h"}
    assert _match_entain_leg({"market": "h2h", "selection": "away"},
                             ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton") == \
        {"market_id": "mb", "entrant_id": "mb-a"}


def test_entain_reads_the_line_off_the_market_not_the_entrant():
    """Entrants are bare "Over"/"Under"; `handicap` lives on the market."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_entain_leg
    assert _match_entain_leg({"market": "total", "selection": "over", "line": 173.5},
                             ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton") == \
        {"market_id": "tp", "entrant_id": "tp-o"}
    assert isinstance(_match_entain_leg({"market": "total", "selection": "over", "line": 999.5},
                                        ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton"), str)


def test_entain_matches_a_line_on_magnitude_because_the_sign_is_one_sided():
    """`handicap` is -5.5 on a market whose entrants are both teams. Which team owns the
    negative number is not stated, so the magnitude is matched and the SIDE comes from
    home_away — guessing the sign would quote the opposite bet at a plausible price."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_entain_leg
    assert _match_entain_leg({"market": "line", "selection": "home", "line": 5.5},
                             ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton") == \
        {"market_id": "ln", "entrant_id": "ln-h"}


def test_entain_honours_the_flag_its_own_pricer_ignores():
    """Entain's pricer priced 12 of 14 markets it had flagged unavailable — including two
    of the impossible quotes. Honouring `same_game_multi_available` here is the
    client-side half of that defence."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_entain_leg
    got = _match_entain_leg({"market": "highest_half", "selection": "1st Half"},
                            ENTAIN_MARKETS, ENTAIN_ENTRANTS, "Melbourne", "Carlton")
    assert isinstance(got, str), "a market flagged not-SGM must not be offered as a leg"


# ─── TAB ────────────────────────────────────────────────────────────────

#: TAB's REAL vocabulary, copied from a live response: names are abbreviated and carry
#: the fixture, and the line lives in the market name. The per-quarter line market is the
#: instructive one — it looks like the match line and is NOT combinable.
TAB_MARKETS = [
    {"id": 1, "name": "AFL WBdg-Coll Hd to Hd", "sameGame": True, "bettingStatus": "Open",
     "propositions": [{"id": 1016, "name": "Wst Bulldogs", "isOpen": True},
                      {"id": 1004, "name": "Collingwood", "isOpen": True}]},
    {"id": 2, "name": "AFL WBdg-Coll TotPtsOU 169.5", "sameGame": True, "bettingStatus": "Open",
     "propositions": [{"id": 7336, "name": "Over 169.5 Pts", "isOpen": True},
                      {"id": 7337, "name": "Under 169.5 Pts", "isOpen": True}]},
    {"id": 3, "name": "AFL WBdg-Coll Line +1.5", "sameGame": True, "bettingStatus": "Open",
     "propositions": [{"id": 6464, "name": "Wst Bulldogs +1.5", "isOpen": True},
                      {"id": 6465, "name": "Collingwood -1.5", "isOpen": True}]},
    {"id": 4, "name": "AFL WBdg-Coll 2ndQLine +0.5", "sameGame": None, "bettingStatus": "Open",
     "propositions": [{"id": 6355, "name": "Wst Bulldogs +0.5", "isOpen": True}]},
]


def test_tab_only_offers_markets_flagged_sameGame():
    """52 of 109 markets were combinable on the match TAB's pricer was verified against.
    A leg from a non-sameGame market is refused by TAB, so it is never proposed."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_tab_leg
    assert _match_tab_leg({"market": "h2h", "selection": "home"},
                          TAB_MARKETS, HOME, AWAY) == 1016
    only_excluded = [m for m in TAB_MARKETS if not m["sameGame"]]
    assert isinstance(_match_tab_leg({"market": "h2h", "selection": "home"},
                                     only_excluded, HOME, AWAY), str)


def test_tab_matches_a_total_by_side_and_line():
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_tab_leg
    assert _match_tab_leg({"market": "total", "selection": "over", "line": 169.5},
                          TAB_MARKETS, HOME, AWAY) == 7336


def test_tab_reads_the_line_out_of_the_market_name():
    """"AFL WBdg-Coll Line +1.5" — the line is not on the proposition."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_tab_leg
    assert _match_tab_leg({"market": "line", "selection": "home", "line": 1.5},
                          TAB_MARKETS, HOME, AWAY) == 6464


def test_tab_excludes_a_per_quarter_line_that_looks_like_the_match_line():
    """The 2nd-quarter line market is shaped exactly like the match line and is NOT
    combinable. `sameGame` is the only thing separating them, which is why it is the
    filter rather than the market name."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _match_tab_leg
    assert _match_tab_leg({"market": "line", "selection": "home", "line": 0.5},
                          TAB_MARKETS, HOME, AWAY) != 6355


def test_tab_matches_abbreviated_team_names():
    """TAB writes "Wst Bulldogs" for Western Bulldogs and "Sydney" for Sydney Swans, so an
    exact comparison fails on most of the league. Matching on the longest distinctive word
    is what makes the resolver work at all — and skipping short words is what stops "st"
    out of "St Kilda" matching half the competition."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _tab_team_matches
    assert _tab_team_matches("Wst Bulldogs", "Western Bulldogs")
    assert _tab_team_matches("Collingwood", "Collingwood")
    assert _tab_team_matches("Sydney", "Sydney Swans")
    assert not _tab_team_matches("Collingwood", "Western Bulldogs")
    assert not _tab_team_matches("St Kilda", "Port Adelaide")


def test_tab_has_a_sport_name_mapping_because_it_has_no_ids():
    from sportsdata_agents.interfaces.sportsboard.sgm_books import TAB_NAMES
    assert TAB_NAMES["afl"] == ("AFL Football", "AFL")


# ─── the set, as a whole ────────────────────────────────────────────────


def test_every_bookmaker_now_resolves():
    """The comparator is only a comparator if the books are actually in it."""
    assert set(_QUOTERS) == set(BOOKMAKERS)


# ── team-name matching: exact equality is not enough ──────────────────────
# Found live 2026-08-27: Kambi's participant is "TCU Horned Frogs" where the fixture
# name is "TCU v North Carolina", so `_norm(a) == _norm(b)` failed and the leg reported
# "no open head-to-head selection" — which reads as a suspended market rather than a
# naming mismatch, and made a scan look like a fixture nobody would price.


def test_a_book_may_carry_the_full_nickname() -> None:
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _team_matches

    assert _team_matches("TCU Horned Frogs", "TCU")
    assert _team_matches("North Carolina Tar Heels", "North Carolina")
    assert _team_matches("Wst Bulldogs", "Wst Bulldogs")


def test_matching_is_on_word_boundaries_not_substrings() -> None:
    """A bare substring test makes "Sydney" match both "Sydney Swans" and "Sydney FC",
    pairing the wrong team's price with the right team's name."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _team_matches

    assert not _team_matches("Newcastle United", "New")
    assert not _team_matches("Port Adelaide", "Adelaide United")


def test_an_ambiguous_team_name_matches_nothing() -> None:
    """Two plausible teams means the resolver does not know which price it is looking
    at, and a wrong leg is a wrong bet at a right-looking price."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _unique_team_match

    outs = [{"participant": "Sydney Swans"}, {"participant": "Sydney FC"}]
    assert _unique_team_match(outs, "Sydney", lambda o: o["participant"]) is None

    one = [{"participant": "Sydney Swans"}, {"participant": "Carlton Blues"}]
    assert _unique_team_match(one, "Sydney", lambda o: o["participant"])["participant"] == "Sydney Swans"


def test_exact_wins_over_a_loose_match() -> None:
    from sportsdata_agents.interfaces.sportsboard.sgm_books import _unique_team_match

    outs = [{"participant": "Adelaide"}, {"participant": "Adelaide Crows"}]
    hit = _unique_team_match(outs, "Adelaide", lambda o: o["participant"])
    assert hit["participant"] == "Adelaide"


def test_the_tab_sport_lookup_uses_the_keys_normalisation() -> None:
    """`_norm` strips underscores, so a raw TAB_NAMES.get(_norm(sport)) could never match
    a key like "australian_rules": the lookup asks for "australianrules". Every AFL
    fixture reported "no TAB sport/competition mapping" while TAB was listing the match.
    Found live 2026-08-27 on Western Bulldogs v Collingwood."""
    from sportsdata_agents.interfaces.sportsboard.sgm_books import (
        _TAB_NAMES_NORMALISED,
        TAB_NAMES,
    )

    assert "australianrules" in _TAB_NAMES_NORMALISED
    assert "rugbyleague" in _TAB_NAMES_NORMALISED
    # every declared sport survives the normalisation, none collide away
    assert len(_TAB_NAMES_NORMALISED) == len(TAB_NAMES)
    assert _TAB_NAMES_NORMALISED["australianrules"] == ("AFL Football", "AFL")
