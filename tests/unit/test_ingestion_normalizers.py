"""M2.1 — feed normalizers (pure, fixture-driven; payload shape captured live 2026-06-10)."""

from __future__ import annotations

import pytest

from sportsdata_agents.operations.ingestion import normalize_nba_odds

pytestmark = pytest.mark.unit

# Trimmed from a real nba_odds_today response (NBA Finals, 2026-06-10).
NBA_PAYLOAD = {
    "games": [
        {
            "gameId": "0042500403",
            "homeTeamId": "1610612752",
            "awayTeamId": "1610612759",
            "markets": [
                {
                    "name": "2way",
                    "books": [
                        {
                            "name": "Novibet",
                            "outcomes": [
                                {"type": "home", "odds": "1.820", "opening_odds": "1.640", "odds_trend": "up"},
                                {"type": "away", "odds": "2.000", "opening_odds": "2.200", "odds_trend": "down"},
                            ],
                            "countryCode": "GR",
                        },
                        {  # the SAME book repeated for another country — must dedupe
                            "name": "Novibet",
                            "outcomes": [
                                {"type": "home", "odds": "1.820"},
                                {"type": "away", "odds": "2.000"},
                            ],
                            "countryCode": "CY",
                        },
                        {
                            "name": "TabAustralia",
                            "outcomes": [
                                {"type": "home", "odds": "1.850"},
                                {"type": "away", "odds": "2.000"},
                            ],
                            "countryCode": "AU",
                        },
                    ],
                },
                {
                    "name": "spread",
                    "books": [
                        {
                            "name": "TabAustralia",
                            "outcomes": [
                                {"type": "home", "odds": "1.900", "spread": "-2"},
                                {"type": "away", "odds": "1.900", "spread": "2"},
                            ],
                        }
                    ],
                },
            ],
        }
    ]
}


def test_normalize_nba_dedupes_books_and_keys_spreads() -> None:
    points = normalize_nba_odds(NBA_PAYLOAD)
    # 2way: Novibet home/away ONCE (CY repeat dropped) + Tab home/away; spread: Tab 2
    assert len(points) == 6
    keys = {p.key for p in points}
    assert len(keys) == 6
    novibet_home = next(p for p in points if p.book == "Novibet" and p.selection == "home")
    assert novibet_home.odds == 1.82
    assert novibet_home.event_external_id == "0042500403"
    assert novibet_home.sport == "nba" and novibet_home.provider == "nba_cdn"
    assert novibet_home.meta["trend"] == "up"
    # the spread line is part of the selection identity
    spread_selections = {p.selection for p in points if p.market == "spread"}
    assert spread_selections == {"home -2", "away 2"}


def test_normalize_nba_skips_junk_not_crashes() -> None:
    junk = {
        "games": [
            {"gameId": "", "markets": []},  # no id → skipped
            {
                "gameId": "G1",
                "markets": [
                    {
                        "name": "2way",
                        "books": [
                            {
                                "name": "X",
                                "outcomes": [
                                    {"type": "home"},  # no odds
                                    {"type": "away", "odds": "not-a-number"},
                                    {"type": "draw", "odds": "0.5"},  # sub-1.01 → junk
                                    {"type": "home", "odds": "1.90"},  # the one good outcome
                                ],
                            }
                        ],
                    }
                ],
            },
        ]
    }
    points = normalize_nba_odds(junk)
    assert [(p.selection, p.odds) for p in points] == [("home", 1.9)]


def test_normalize_empty_payload() -> None:
    assert normalize_nba_odds({}) == []
    assert normalize_nba_odds({"games": None}) == []


# ── Sportsbet (shape captured live 2026-06-11, AFL competition 4165) ──────

SPORTSBET_PAYLOAD = [
    {
        "groupName": "Head to Head",
        "events": [
            {
                "id": 10551746,
                "displayName": "Western Bulldogs v Adelaide Crows",
                "bettingStatus": "PRICED",
                "startTime": 1781170205,
                "primaryMarket": {
                    "name": "Head to Head",
                    "marketSort": "HH",
                    "selections": [
                        {"name": "Western Bulldogs", "resultType": "H", "price": {"winPrice": 1.73}},
                        {"name": "Adelaide Crows", "resultType": "A", "price": {"winPrice": 2.12}},
                    ],
                },
            },
            {  # suspended events carry no usable prices
                "id": 10551747,
                "displayName": "Geelong Cats v Gold Coast SUNS",
                "bettingStatus": "SUSPENDED",
                "primaryMarket": {"marketSort": "HH", "selections": [{"resultType": "H"}]},
            },
        ],
    }
]


def test_normalize_sportsbet_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_sportsbet_matches

    points = normalize_sportsbet_matches(SPORTSBET_PAYLOAD, sport="afl")
    assert len(points) == 2  # the suspended event is skipped entirely
    home = next(p for p in points if p.selection == "home")
    assert (home.provider, home.book, home.sport) == ("sportsbet", "Sportsbet", "afl")
    assert home.event_external_id == "10551746"
    assert home.market == "h2h" and home.odds == 1.73
    assert home.meta["team"] == "Western Bulldogs"
    assert {p.selection for p in points} == {"home", "away"}
    # junk shapes never crash
    assert normalize_sportsbet_matches({}, sport="afl") == []
    assert normalize_sportsbet_matches(None, sport="afl") == []


# ── TAB (shape captured live 2026-06-11, "AFL Football"/"AFL") ────────────

TAB_PAYLOAD = {
    "matches": [
        {
            "id": "WBdvAdl",
            "name": "Wst Bulldogs v Adelaide",
            "startTime": "2026-06-11T09:30:00.000Z",
            "markets": [
                {
                    "betOption": "Head To Head",
                    "name": "AFL WBdg-Adl Hd to Hd",
                    "propositions": [
                        {"name": "Wst Bulldogs", "returnWin": 1.74, "bettingStatus": "Open",
                         "position": "HOME"},
                        {"name": "Adelaide", "returnWin": 2.1, "bettingStatus": "Open", "position": "AWAY"},
                        {"name": "Ghost", "returnWin": 3.0, "bettingStatus": "Suspended", "position": "AWAY"},
                    ],
                },
                {  # other top markets (lines, totals) are not ingested yet
                    "betOption": "Line",
                    "propositions": [{"name": "X", "returnWin": 1.9, "bettingStatus": "Open"}],
                },
            ],
        }
    ]
}


def test_normalize_tab_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_tab_competition

    points = normalize_tab_competition(TAB_PAYLOAD, sport="afl")
    assert len(points) == 2  # suspended proposition + non-H2H market skipped
    home = next(p for p in points if p.selection == "home")
    assert (home.provider, home.book) == ("tab", "TAB")
    assert home.event_external_id == "WBdvAdl"
    assert home.market == "h2h" and home.odds == 1.74
    assert home.event_name == "Wst Bulldogs v Adelaide"
    assert normalize_tab_competition([], sport="afl") == []
    assert normalize_tab_competition({}, sport="afl") == []


def test_feed_registry_covers_all_providers() -> None:
    from sportsdata_agents.operations.ingestion import FEEDS

    assert {"nba_odds", "sportsbet_afl_h2h", "tab_afl_h2h"} <= set(FEEDS)
    assert FEEDS["sportsbet_afl_h2h"].mcp_groups == ("sportsbet.sports",)
    assert FEEDS["tab_afl_h2h"].mcp_groups == ("tab.sports",)
    assert FEEDS["tab_afl_h2h"].arguments == {"sport": "AFL Football", "competition": "AFL",
                                              "numTopMarkets": 1}
