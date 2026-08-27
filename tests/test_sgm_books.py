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
    assert "tab" not in _QUOTERS, "TAB is documented as unwired; it must not quote"


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
