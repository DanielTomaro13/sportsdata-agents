"""The ingest prop tagger: any book's ladder-shaped props gain structured meta."""

from __future__ import annotations

from sportsdata_agents.operations.ingestion.prop_tagger import tag_prop


def test_market_prop_with_over_under_selection() -> None:
    meta = tag_prop("marcus bontempelli disposals", "over 24.5", {})
    assert meta == {"player": "Marcus Bontempelli", "stat": "disposals",
                    "stat_line": 24.5, "line_type": "over", "prop_tagged": True}
    under = tag_prop("Marcus Bontempelli - Disposals", "under 24.5", {})
    assert under["line_type"] == "under" and under["stat_line"] == 24.5


def test_nplus_selection_form() -> None:
    meta = tag_prop("player points markets", "nathan cleary 20+ points", {})
    # 20+ means 20 or more: the over side of a 19.5 line
    assert meta["player"] == "Nathan Cleary" and meta["stat"] == "points"
    assert meta["stat_line"] == 19.5 and meta["line_type"] == "over"


def test_longest_stat_wins() -> None:
    meta = tag_prop("latrell mitchell try assists", "over 0.5", {})
    assert meta["stat"] == "try assists"  # not "tries" mis-split


def test_already_tagged_and_non_props_pass_through() -> None:
    dabble = {"player": "Someone", "stat": "points", "stat_line": 10.5, "line_type": "over"}
    assert tag_prop("points", "over 10.5", dabble) is dabble
    assert tag_prop("h2h", "home", {}) == {}
    assert tag_prop("total", "over 224.5", {}) == {}          # no player name
    assert tag_prop("points handicap -13.5", "lionheart", {}) == {}  # esports handicap, not a ladder
    assert tag_prop("world cup daily specials", "all games over 1.5 goals in regular time", {}) == {}
