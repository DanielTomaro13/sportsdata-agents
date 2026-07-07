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


def test_form_prose_parser_real_sentences() -> None:
    """The one grammar covering TAB formComments and Sportsbet AU overviews."""
    import datetime as dt

    from sportsdata_agents.operations.ingestion.form import parse_comment_runs

    now = dt.datetime(2026, 7, 7, tzinfo=dt.UTC)
    tab = ("First-up won by 2.5 len at Scone Red Crown May 31 over 900m. "
           "Second run 3rd of 7 at Warwick Farm 2yo F Osc on June 24 over 1100m.")
    runs = parse_comment_runs(tab, now)
    assert [(r["position"], r["field_size"]) for r in runs] == [(1, 8), (3, 7)]
    assert runs[0]["age_days"] == 37.0 and runs[1]["age_days"] == 13.0
    sb = ("First-up ran second last of 7 at Yil All Weather on June 16 over 1500m. "
          "Second run from a spell 5th of 8 at Kartepe on June 23 over 1400m.")
    assert [(r["position"], r["field_size"]) for r in parse_comment_runs(sb, now)] \
        == [(6, 7), (5, 8)]
    # a December date "in the future" reads as LAST year
    old = parse_comment_runs("4th of 9 at Sale on December 20 over 515m.", now)
    assert old and 190 < old[0]["age_days"] < 210
    assert parse_comment_runs("Trainer expects improvement.", now) == []


def test_tab_market_side_ladders_tag() -> None:
    """TAB inverts the ladder: market "25+ Disposals", selection = player."""
    from sportsdata_agents.operations.ingestion.prop_tagger import tag_prop

    meta = tag_prop("25+ Disposals", "Nick Daicos", {})
    assert meta["prop_tagged"] and meta["player"] == "Nick Daicos"
    assert meta["stat"] == "disposals" and meta["stat_line"] == 24.5
    assert meta["line_type"] == "over"
    meta = tag_prop("To Kick 2+ Goals", "Charlie Curnow", {})
    assert meta["stat"] == "goals" and meta["stat_line"] == 1.5
    # a numbered selection is not a player name — never tag it
    assert "prop_tagged" not in tag_prop("25+ Disposals", "over 24.5", {})
