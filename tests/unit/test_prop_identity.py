"""Player props across books: who the bet is on, and whether it is the same bet.

The structured extraction already existed — the tagger writes player/stat/stat_line/
line_type into meta so any book's props join the stat-ladder pipeline. What did not
exist was the ability to say "these two rows, at two books, are the same bet", which is
what a cross-book prop comparison needs.

Two things are pinned here: the tagger must not invent players, and the comparison must
not conflate different bets.
"""

from __future__ import annotations

import pytest

from sportsdata_agents.operations.ingestion.prop_tagger import (
    player_names_match,
    same_prop,
    tag_prop,
)

pytestmark = pytest.mark.unit


# ─── the tagger must not invent a player ────────────────────────────────


@pytest.mark.parametrize("selection", ["home", "away", "draw", "neither"])
def test_a_side_is_never_a_player(selection: str) -> None:
    """THE bug this guard was written for. Ladbrokes answers its "anytime try scorer"
    market with home/away, and the tagger recorded `player: "Away"` with a 0.5 tries
    line — a phantom ladder, which the module's own docstring calls the failure it most
    wants to avoid. Measured in the warehouse 2026-08-27; the real player name was in
    `meta["team"]` all along."""
    assert "player" not in tag_prop("anytime try scorer", selection, {})


@pytest.mark.parametrize("selection", ["no goal", "any other player", "not scored"])
def test_a_catch_all_bucket_is_never_a_player(selection: str) -> None:
    """"Any other player" is the residual of a scorer market, not a person. Pricing it
    as a named player's ladder invents a bet nobody offered."""
    assert "player" not in tag_prop("anytime goalscorer", selection, {})


def test_a_real_player_is_still_tagged() -> None:
    """The guard must not cost the thing the tagger is for."""
    tagged = tag_prop("anytime try scorer", "Nick Daicos", {})
    assert tagged["player"] == "Nick Daicos"
    assert tagged["stat"] == "tries"
    assert tagged["stat_line"] == 0.5
    assert tagged["line_type"] == "over"


def test_an_already_tagged_point_passes_through() -> None:
    """Dabble carries the structure natively; re-deriving it would be a second chance
    to get it wrong."""
    native = {"player": "Riley Thilthorpe", "stat": "tackles",
              "stat_line": 3.5, "line_type": "over"}
    assert tag_prop("first goalscorer", "riley thilthorpe over 3.5", native)["player"] == \
        "Riley Thilthorpe"


# ─── who the bet is on ──────────────────────────────────────────────────


def test_books_spell_the_same_person_differently() -> None:
    assert player_names_match("Arango E", "Emiliana Arango")       # TAB: surname-initial
    assert player_names_match("N Daicos", "Nick Daicos")
    assert player_names_match("Riley Thilthorpe (ADE)", "Riley Thilthorpe")  # club appended


def test_two_players_sharing_a_surname_are_not_the_same_person() -> None:
    """Why this is not a substring test. The Daicos brothers play for the same club, so
    a surname match would pair one brother's price with the other's line — a wrong bet
    at a right-looking number."""
    assert not player_names_match("Nick Daicos", "Josh Daicos")


# ─── is it the same bet ─────────────────────────────────────────────────


def prop(**kw):
    base = {"player": "Nick Daicos", "stat": "disposals",
            "stat_line": 24.5, "line_type": "over"}
    base.update(kw)
    return base


def test_the_same_bet_at_two_books_matches() -> None:
    assert same_prop(prop(player="N Daicos"), prop())


def test_a_different_line_is_a_different_bet() -> None:
    """No tolerance on the line, deliberately. 24.5 and 25.5 are different bets, and a
    tolerance here would silently compare them — the exact failure this prevents."""
    assert not same_prop(prop(), prop(stat_line=25.5))


def test_the_other_side_is_a_different_bet() -> None:
    assert not same_prop(prop(), prop(line_type="under"))


def test_a_different_stat_is_a_different_bet() -> None:
    assert not same_prop(prop(), prop(stat="kicks"))


def test_an_untagged_point_never_matches_anything() -> None:
    """Missing structure is not a wildcard. A row the tagger declined to tag must not
    become comparable by default."""
    assert not same_prop({}, prop())
    assert not same_prop(prop(stat_line=None), prop())
