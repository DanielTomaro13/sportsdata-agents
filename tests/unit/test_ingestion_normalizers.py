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


# ── Unibet/Kambi (captured live 2026-06-11) ───────────────────────────────


def test_normalize_unibet_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_unibet_matches

    payload = {
        "events": [
            {
                "event": {"id": 1025627732, "name": "Western Bulldogs - Adelaide",
                          "homeName": "Western Bulldogs", "awayName": "Adelaide",
                          "start": "2026-06-11T09:30:00Z"},
                "betOffers": [
                    {
                        "betOfferType": {"id": 2, "name": "Head to Head", "englishName": "Match"},
                        "criterion": {"lifetime": "FULL_TIME"},
                        "outcomes": [
                            {"type": "OT_ONE", "odds": 1730, "participant": "Western Bulldogs",
                             "status": "OPEN"},
                            {"type": "OT_TWO", "odds": 2150, "participant": "Adelaide", "status": "OPEN"},
                            {"type": "OT_ONE", "odds": 900, "participant": "junk sub-1.01"},  # 0.9 → skip
                        ],
                    },
                    {"betOfferType": {"name": "Handicap"}, "outcomes": [{"type": "OT_ONE", "odds": 1900}]},
                ],
            }
        ]
    }
    points = normalize_unibet_matches(payload, sport="afl")
    assert [(p.selection, p.odds) for p in points] == [("home", 1.73), ("away", 2.15)]
    assert points[0].provider == "unibet" and points[0].event_external_id == "1025627732"
    assert normalize_unibet_matches([], sport="afl") == []


# ── BetR (captured live 2026-06-11, category 43735) ───────────────────────


def test_normalize_betr_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_betr_category

    payload = {
        "MasterCategories": [{
            "Categories": [{
                "MasterEvents": [{
                    "MasterEventId": 2094343,
                    "MasterEventName": "Western Bulldogs v Adelaide Crows",
                    "MinAdvertisedStartTime": "2026-06-11T09:30:00.0000000Z",
                    "Markets": [
                        {"EventName": "Match Result (Western Bulldogs v Adelaide Crows)",
                         "OutcomeName": "Western Bulldogs", "Price": 1.74, "MarketDesc": "Win"},
                        {"EventName": "Match Result (Western Bulldogs v Adelaide Crows)",
                         "OutcomeName": "Adelaide Crows", "Price": 2.1, "MarketDesc": "Win"},
                        {"EventName": "Total Points Over/Under", "OutcomeName": "Over",
                         "Price": 1.9, "MarketDesc": "Win"},  # not Match Result → skip
                    ],
                }]
            }]
        }]
    }
    points = normalize_betr_category(payload, sport="afl")
    assert [(p.selection, p.odds) for p in points] == [("home", 1.74), ("away", 2.1)]
    assert points[0].event_external_id == "2094343" and points[0].book == "BetR"


# ── Entain (captured live 2026-06-11) ─────────────────────────────────────


def test_normalize_entain_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_entain_events

    payload = {
        "events": {"EV1": {"name": "Carlton v Hawthorn", "match_status": "BettingOpen"}},
        "markets": {
            "MK1": {"event_id": "EV1", "name": "Head To Head"},
            "MK2": {"event_id": "EV1", "name": "Most Goals"},  # prop → skip
        },
        "entrants": {
            "EN1": {"name": "Carlton", "market_id": "MK1"},
            "EN2": {"name": "Hawthorn", "market_id": "MK1"},
            "EN3": {"name": "Someone", "market_id": "MK2"},
        },
        "prices": {
            "EN1:prod:": {"odds": {"numerator": 3, "denominator": 4}},   # 1.75
            "EN2:prod:": {"odds": {"numerator": 21, "denominator": 10}},  # 3.1
            "EN3:prod:": {"odds": {"numerator": 1, "denominator": 2}},
        },
    }
    points = normalize_entain_events(payload, sport="afl")
    by_sel = {p.selection: p.odds for p in points}
    assert by_sel == {"home": 1.75, "away": 3.1}
    assert all(p.provider == "entain" and p.event_external_id == "EV1" for p in points)


# ── Pinnacle (captured live 2026-06-11, league 5448) ──────────────────────


def test_normalize_pinnacle_h2h_converts_american() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import (
        american_to_decimal,
        normalize_pinnacle_league,
    )

    assert american_to_decimal(194) == pytest.approx(2.94)
    assert american_to_decimal(-257) == pytest.approx(1.389, abs=1e-3)

    payload = {
        "matchups": [{
            "id": 1631723953,
            "startTime": "2026-06-13T09:35:00+00:00",
            "participants": [{"name": "St Kilda", "alignment": "home"},
                             {"name": "GWS", "alignment": "away"}],
        }],
        "markets": {
            "1631723953": [
                {"type": "moneyline", "period": 0, "isAlternate": False, "status": "open",
                 "key": "s;0;m",
                 "prices": [{"designation": "home", "price": 194},
                            {"designation": "away", "price": -257}]},
                {"type": "spread", "period": 0, "prices": [{"designation": "home", "price": -105}]},
                {"type": "moneyline", "period": 1,  # first-half line → skip
                 "prices": [{"designation": "home", "price": 150}]},
            ]
        },
    }
    points = normalize_pinnacle_league(payload, sport="afl")
    by_sel = {p.selection: p.odds for p in points}
    assert by_sel["home"] == pytest.approx(2.94)
    assert by_sel["away"] == pytest.approx(1.389, abs=1e-3)
    assert points[0].book == "Pinnacle" and points[0].event_name == "St Kilda v GWS"


# ── PointsBet (captured live 2026-06-11, event detail) ────────────────────


def test_normalize_pointsbet_h2h() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_pointsbet_events

    payload = {
        "events": [{
            "key": 2765418, "name": "Western Bulldogs v Adelaide",
            "homeTeam": "Western Bulldogs", "awayTeam": "Adelaide",
            "startsAt": "2026-06-11T09:30:00Z",
            "fixedOddsMarkets": [
                {"eventClass": "To Kick Goals", "outcomes": [{"name": "Someone 1+", "price": 3.6}]},
                {"eventClass": "Match Result", "outcomes": [
                    {"name": "Western Bulldogs", "price": 1.74},
                    {"name": "Adelaide", "price": 2.1},
                ]},
            ],
        }]
    }
    points = normalize_pointsbet_events(payload, sport="afl")
    assert [(p.selection, p.odds) for p in points] == [("home", 1.74), ("away", 2.1)]
    assert points[0].provider == "pointsbet" and points[0].event_external_id == "2765418"


# ── Betfair exchange ──────────────────────────────────────────────────────


def test_normalize_betfair_match_odds() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_betfair_by_event

    payload = {
        "eventTypes": [{
            "eventNodes": [{
                "eventId": 34763300,
                "event": {"eventName": "Western Bulldogs v Adelaide"},
                "marketNodes": [
                    {"description": {"marketType": "MATCH_ODDS"}, "state": {"status": "OPEN"},
                     "runners": [
                        {"description": {"runnerName": "Western Bulldogs"},
                         "exchange": {"availableToBack": [{"price": 1.78, "size": 1200}],
                                      "availableToLay": [{"price": 1.8}]}},
                        {"description": {"runnerName": "Adelaide"},
                         "exchange": {"availableToBack": [{"price": 2.24, "size": 600}]}},
                     ]},
                    {"description": {"marketType": "TOTAL_POINTS"}, "runners": []},
                ],
            }]
        }]
    }
    points = normalize_betfair_by_event(payload, sport="afl")
    assert [(p.selection, p.odds) for p in points] == [("home", 1.78), ("away", 2.24)]
    assert points[0].meta["lay"] == 1.8
    assert points[0].book == "Betfair"


def test_unibet_nrl_overtime_lifetime_and_entain_vs_separator() -> None:
    """NRL h2h on Kambi quotes 'Including Overtime' (FULL_TIME_OVERTIME) — it IS the
    match market; Entain event names use ' vs ' not ' v '."""
    from sportsdata_agents.operations.ingestion.normalizers import (
        _side_from_event_name,
        normalize_unibet_matches,
    )

    payload = {
        "events": [{
            "event": {"id": 9, "name": "South Sydney Rabbitohs - Brisbane Broncos"},
            "betOffers": [{
                "betOfferType": {"name": "Head to Head"},
                "criterion": {"lifetime": "FULL_TIME_OVERTIME", "label": "Including Overtime"},
                "outcomes": [
                    {"type": "OT_ONE", "odds": 2400, "participant": "South Sydney"},
                    {"type": "OT_TWO", "odds": 1570, "participant": "Brisbane"},
                ],
            }],
        }]
    }
    points = normalize_unibet_matches(payload, sport="nrl")
    assert [(p.selection, p.odds) for p in points] == [("home", 2.4), ("away", 1.57)]

    assert _side_from_event_name("Gold Coast Suns", "Gold Coast Suns vs Hawthorn") == "home"
    assert _side_from_event_name("Hawthorn", "Gold Coast Suns vs Hawthorn") == "away"
