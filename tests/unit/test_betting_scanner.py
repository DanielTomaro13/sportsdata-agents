"""Scoring a cross-book comparison — the easiest place in the plane to fool yourself.

Every test here pins a decision that could plausibly have gone the other way, because
each wrong choice produces a plausible-looking number rather than an error.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.betting.scanner import (
    Candidate,
    candidates_from_comparison,
    consensus_of,
    edge_of,
)

pytestmark = pytest.mark.unit


def comparison(**odds: float) -> dict:
    return {
        "legs": [{"market": "h2h", "selection": "Bulldogs"}, {"market": "total", "line": 170.5}],
        "quotes": [{"book": b, "book_odds": o, "warnings": []} for b, o in odds.items()],
    }


# ─── the consensus ──────────────────────────────────────────────────────


def test_consensus_is_averaged_in_probability_space() -> None:
    """Odds are a reciprocal scale. The midpoint of 2.0 and 10.0 is NOT 6.0 — that
    implies a 16.7% chance, while the honest midpoint of 50% and 10% is 30%. Averaging
    in odds space flatters every longshot on the board."""
    assert consensus_of([2.0, 10.0]) == pytest.approx(10 / 3, rel=1e-6)


def test_consensus_uses_the_median_not_the_mean() -> None:
    """One broken quote — a mismatched leg, a capped payout — moves a mean a long way
    and a median barely at all."""
    sane = consensus_of([3.0, 3.1, 3.2])
    with_outlier = consensus_of([3.0, 3.1, 3.2, 900.0])
    assert with_outlier == pytest.approx(sane, rel=0.10)


def test_an_empty_field_has_no_consensus() -> None:
    with pytest.raises(ValueError, match="no other books"):
        consensus_of([])


# ─── the edge ───────────────────────────────────────────────────────────


def test_relative_edge_is_the_ratio_to_the_field() -> None:
    assert edge_of(odds=3.3, consensus_odds=3.0) == pytest.approx(0.10)


def test_relative_edge_is_negative_below_the_field() -> None:
    assert edge_of(odds=2.7, consensus_odds=3.0) == pytest.approx(-0.10)


def test_ev_edge_uses_the_ordinary_definition() -> None:
    """p * o - 1, the same definition quant.value uses."""
    # consensus 4.0 → implied 0.25; at odds 5.0 that is 0.25*5 - 1 = 0.25
    assert edge_of(odds=5.0, consensus_odds=4.0, basis="ev") == pytest.approx(0.25)


def test_ev_edge_shrinks_as_the_assumed_overround_grows() -> None:
    """Under-stating the field's margin over-states the edge, in exactly this direction —
    which is why the parameter exists and why zero warns."""
    generous = edge_of(odds=5.0, consensus_odds=4.0, basis="ev", assumed_overround=0.0)
    honest = edge_of(odds=5.0, consensus_odds=4.0, basis="ev", assumed_overround=0.06)
    assert honest < generous


def test_ev_with_no_overround_assumption_warns(caplog) -> None:
    with caplog.at_level("WARNING"):
        edge_of(odds=5.0, consensus_odds=4.0, basis="ev", assumed_overround=0.0)
    assert any("overstates" in r.getMessage() for r in caplog.records)


def test_ev_without_an_overround_is_the_relative_number_wearing_an_ev_label() -> None:
    """Algebraically identical, not merely close: (1/c)*o - 1 IS o/c - 1. Asking for EV
    without supplying a margin gets the relative figure mislabelled — which is exactly
    what edge_basis exists to prevent, hence the warning."""
    rel = edge_of(odds=3.3, consensus_odds=3.0, basis="relative")
    ev = edge_of(odds=3.3, consensus_odds=3.0, basis="ev", assumed_overround=0.0)
    assert rel == pytest.approx(ev)


def test_the_bases_diverge_once_a_margin_is_supplied() -> None:
    """EV only becomes a different MEASUREMENT when it is told what the field is
    carrying. 3% relative is not 3% EV at any honest overround."""
    rel = edge_of(odds=3.3, consensus_odds=3.0, basis="relative")
    ev = edge_of(odds=3.3, consensus_odds=3.0, basis="ev", assumed_overround=0.06)
    assert ev < rel


def test_a_non_price_is_refused() -> None:
    with pytest.raises(ValueError, match="not usable"):
        edge_of(odds=1.0, consensus_odds=3.0)


# ─── scoring a whole comparison ─────────────────────────────────────────


def test_each_book_is_scored_against_the_others_not_itself() -> None:
    """A book included in its own consensus drags that number toward itself, shrinking
    its own apparent edge — the outlier partly hides."""
    cands = candidates_from_comparison(
        comparison(sportsbet=3.6, tab=3.0, unibet=3.0), fixture_id="f1")
    best = cands[0]
    assert best.book == "sportsbet"
    assert best.consensus_odds == pytest.approx(3.0)   # not pulled up by the 3.6
    assert best.edge == pytest.approx(0.20)
    assert best.books_in_consensus == 2


def test_candidates_come_back_best_first() -> None:
    cands = candidates_from_comparison(
        comparison(sportsbet=3.6, tab=3.0, unibet=2.8), fixture_id="f1")
    assert [c.book for c in cands] == ["sportsbet", "tab", "unibet"]
    assert cands[0].edge > cands[-1].edge


def test_one_book_cannot_produce_a_candidate() -> None:
    """With no field there is nothing to measure against, and inventing a benchmark from
    a single quote would be inventing the edge."""
    assert candidates_from_comparison(comparison(sportsbet=3.6), fixture_id="f1") == []


def test_kambis_capped_payout_never_enters_a_consensus() -> None:
    """1001.0 is Kambi's ceiling, not a price. Letting it in would invent an edge for
    every other book on the board."""
    cands = candidates_from_comparison(
        comparison(sportsbet=3.0, tab=3.0, unibet=1001.0), fixture_id="f1")
    assert {c.book for c in cands} == {"sportsbet", "tab"}
    assert all(c.consensus_odds == pytest.approx(3.0) for c in cands)


def test_a_book_you_cannot_place_at_still_informs_the_consensus() -> None:
    """The right way round: a book outside `books` is not a candidate, but it is still
    evidence about what the bet is worth."""
    cands = candidates_from_comparison(
        comparison(sportsbet=3.6, tab=3.0, unibet=3.0),
        fixture_id="f1", books={"sportsbet"})
    assert [c.book for c in cands] == ["sportsbet"]
    assert cands[0].books_in_consensus == 2       # tab and unibet still counted
    assert cands[0].consensus_odds == pytest.approx(3.0)


def test_the_basis_travels_with_the_candidate() -> None:
    """A ledger that mixes bases holds numbers that cannot be compared to each other."""
    for basis in ("relative", "ev"):
        cands = candidates_from_comparison(
            comparison(sportsbet=3.6, tab=3.0), fixture_id="f1", basis=basis)  # type: ignore[arg-type]
        assert cands[0].edge_basis == basis


def test_the_legs_are_restated_on_every_candidate() -> None:
    """Four of the pricers can price a DIFFERENT bet than the one requested, so the legs
    travel with the answer rather than being assumed from the request."""
    cands = candidates_from_comparison(comparison(sportsbet=3.6, tab=3.0), fixture_id="f1")
    assert all(len(c.legs) == 2 for c in cands)


def test_a_summary_reads_as_a_sentence() -> None:
    c = Candidate(book="sportsbet", fixture_id="f1", legs=[{}], odds=3.6,
                  consensus_odds=3.0, edge=0.2, edge_basis="relative", books_in_consensus=2)
    assert "sportsbet 3.60" in c.summary()
    assert "+20.00% relative" in c.summary()
