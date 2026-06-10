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
    # EVERYTHING is captured: h2h pair + the Line market's outcome (suspended skipped)
    by_key = {(p.market, p.selection) for p in points}
    assert by_key == {("h2h", "home"), ("h2h", "away"), ("spread", "x")}
    home = next(p for p in points if p.selection == "home")
    assert (home.provider, home.book) == ("tab", "TAB")
    assert home.event_external_id == "WBdvAdl"
    assert home.market == "h2h" and home.odds == 1.74
    assert home.event_name == "Wst Bulldogs v Adelaide"
    assert normalize_tab_competition([], sport="afl") == []
    assert normalize_tab_competition({}, sport="afl") == []


def test_feed_registry_is_discovery_driven() -> None:
    from sportsdata_agents.operations.ingestion import FEEDS

    assert "nba_odds" not in FEEDS  # the CDN aggregator stays out: books of record only
    hot = {"sportsbet_all", "tab_all", "unibet_all", "entain_all", "pinnacle_all",
           "pointsbet_all", "betr_all", "fanduel_us", "fanduel_racing_win"}
    books = {"sportsbet_books", "tab_books", "unibet_books", "pinnacle_books", "pointsbet_books"}
    assert set(FEEDS) == hot | books  # discovery hot tier + hourly full-book tier
    assert all(FEEDS[n].fetch is not None for n in hot | books)  # discovery, not fixed ids
    assert all(FEEDS[n].interval_s == 3600 for n in books)


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
    by_key = {(p.market, p.selection): p.odds for p in points}
    # h2h pair + the Handicap offer (canonical: handicap → spread) — nothing dropped
    assert by_key == {("h2h", "home"): 1.73, ("h2h", "away"): 2.15, ("spread", "home"): 1.9}
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
    by_key = {(p.market, p.selection): p.odds for p in points}
    # the Over/Under row is captured too (canonical: total), not skipped
    assert by_key == {("h2h", "home"): 1.74, ("h2h", "away"): 2.1, ("total", "over"): 1.9}
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
    by_key = {(p.market, p.selection): p.odds for p in points}
    # the Most Goals prop is captured under its own name — normalization, not exclusion
    assert by_key == {("h2h", "home"): 1.75, ("h2h", "away"): 3.1, ("most goals", "someone"): 1.5}
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
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key[("h2h", "home")] == pytest.approx(2.94)
    assert by_key[("h2h", "away")] == pytest.approx(1.389, abs=1e-3)
    # the first-half moneyline is captured with its period suffix, not dropped
    assert by_key[("h2h p1", "home")] == pytest.approx(2.5)
    # the spread rides too (capture-everything; this fixture's spread has no points)
    assert by_key[("spread", "home")] == pytest.approx(1.952, abs=1e-3)
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
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key[("h2h", "home")] == 1.74 and by_key[("h2h", "away")] == 2.1
    # the prop market is captured under its own name with its line
    assert by_key[("to kick goals", "someone 1+")] == 3.6
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


# ── FanDuel sportsbook + racing, filters, Pinnacle lines (live 2026-06-11) ──


def test_normalize_fanduel_moneyline() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_fanduel_pages

    payload = {
        "pages": [{
            "events": {"35676888": {"eventId": 35676888, "name": "San Antonio Spurs @ New York Knicks"}},
            "markets": {
                "734.171306686": {
                    "marketType": "MONEY_LINE", "marketStatus": "OPEN", "eventId": 35676888,
                    "marketTime": "2026-06-11T00:40:00.000Z",
                    "runners": [
                        {"runnerName": "San Antonio Spurs", "runnerStatus": "ACTIVE",
                         "result": {"type": "AWAY"},
                         "winRunnerOdds": {"trueOdds": {"decimalOdds": {"decimalOdds": 2.08}}}},
                        {"runnerName": "New York Knicks", "runnerStatus": "ACTIVE",
                         "result": {"type": "HOME"},
                         "winRunnerOdds": {"trueOdds": {"decimalOdds": {"decimalOdds": 1.8}}}},
                    ],
                },
                "x": {"marketType": "TOTAL_POINTS_(OVER/UNDER)", "eventId": 35676888, "runners": []},
            },
        }]
    }
    points = normalize_fanduel_pages(payload, sport="nba")
    by_sel = {p.selection: p.odds for p in points}
    assert by_sel == {"away": 2.08, "home": 1.8}
    assert points[0].book == "FanDuel" and points[0].event_external_id == "35676888"


def test_normalize_fanduel_races() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_fanduel_races

    payload = {
        "races": [{
            "tvgRaceId": 3989534, "raceNumber": "8", "isGreyhound": False,
            "postTime": "2026-06-10T20:33:00Z", "track": {"name": "Finger Lakes"},
            "bettingInterests": [
                {"biNumber": 3, "currentOdds": {"numerator": 1, "denominator": None},
                 "runners": [{"horseName": "Observer", "scratched": False}]},
                {"biNumber": 7, "currentOdds": {"numerator": 5, "denominator": 2},
                 "runners": [{"horseName": "Runner B", "scratched": False}]},
                {"biNumber": 9, "currentOdds": {"numerator": 4, "denominator": None},
                 "runners": [{"horseName": "Scratchy", "scratched": True}]},  # scratched → skip
            ],
        }]
    }
    points = normalize_fanduel_races(payload)
    assert [(p.selection, p.odds) for p in points] == [("3", 2.0), ("7", 3.5)]
    assert points[0].sport == "horse_racing" and points[0].market == "win"
    assert points[0].event_external_id == "3989534"
    assert points[0].event_name == "Finger Lakes R8"
    assert points[0].meta["runner"] == "Observer"


def test_unibet_group_and_entain_competition_filters() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import (
        normalize_entain_events,
        normalize_unibet_matches,
    )

    kambi = {
        "events": [
            {"event": {"id": 1, "name": "A - B", "group": "NBA"},
             "betOffers": [{"betOfferType": {"name": "Head to Head"}, "criterion": {},
                            "outcomes": [{"type": "OT_ONE", "odds": 1500}]}]},
            {"event": {"id": 2, "name": "C - D", "group": "WNBA"},
             "betOffers": [{"betOfferType": {"name": "Head to Head"}, "criterion": {},
                            "outcomes": [{"type": "OT_ONE", "odds": 1500}]}]},
        ]
    }
    points = normalize_unibet_matches(kambi, sport="nba", only_group="NBA")
    assert [p.event_external_id for p in points] == ["1"]

    entain = {
        "events": {
            "E1": {"name": "Spurs vs Knicks", "match_status": "BettingOpen", "competition": {"name": "NBA"}},
            "E2": {"name": "Lynx vs Sky", "match_status": "BettingOpen", "competition": {"name": "WNBA"}},
        },
        "markets": {"M1": {"event_id": "E1", "name": "Match Betting"},
                    "M2": {"event_id": "E2", "name": "Match Betting"}},
        "entrants": {"N1": {"name": "Spurs", "market_id": "M1"},
                     "N2": {"name": "Lynx", "market_id": "M2"}},
        "prices": {"N1:p:": {"odds": {"numerator": 1, "denominator": 1}},
                   "N2:p:": {"odds": {"numerator": 1, "denominator": 1}}},
    }
    points = normalize_entain_events(entain, sport="nba", only_competition="NBA")
    assert [p.event_external_id for p in points] == ["E1"]


def test_pinnacle_captures_spread_and_total_lines() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_pinnacle_league

    payload = {
        "matchups": [{"id": 1, "participants": [{"name": "X", "alignment": "home"},
                                                {"name": "Y", "alignment": "away"}]}],
        "markets": {"1": [
            {"type": "spread", "period": 0, "status": "open",
             "prices": [{"designation": "home", "price": -110, "points": -1.5},
                        {"designation": "away", "price": -110, "points": 1.5}]},
            {"type": "total", "period": 0, "status": "open",
             "prices": [{"designation": "over", "price": -105, "points": 220.5},
                        {"designation": "under", "price": -115, "points": 220.5}]},
            {"type": "total", "period": 0, "status": "open",
             "prices": [{"designation": "over", "price": -105}]},  # no line → skipped
        ]},
    }
    points = normalize_pinnacle_league(payload, sport="nba")
    sels = {(p.market, p.selection) for p in points}
    # the line-less total is ALSO captured now (selection without a suffix)
    assert sels == {("spread", "home -1.5"), ("spread", "away 1.5"),
                    ("total", "over 220.5"), ("total", "under 220.5"), ("total", "over")}
    over = next(p for p in points if p.selection == "over 220.5")
    assert over.odds == pytest.approx(1.952, abs=1e-3)  # -105 American


def test_unibet_captures_line_and_totals_with_lines() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_unibet_matches

    payload = {
        "events": [{
            "event": {"id": 5, "name": "A - B"},
            "betOffers": [
                {"betOfferType": {"name": "Line"}, "criterion": {},
                 "outcomes": [
                     {"type": "OT_ONE", "odds": 1880, "line": -4500, "label": "A"},
                     {"type": "OT_TWO", "odds": 1920, "line": 4500, "label": "B"},
                 ]},
                {"betOfferType": {"name": "Totals"}, "criterion": {},
                 "outcomes": [
                     {"type": "OT_OVER", "odds": 1900, "line": 165500, "label": "Over"},
                     {"type": "OT_UNDER", "odds": 1900, "line": 165500, "label": "Under"},
                 ]},
            ],
        }]
    }
    points = normalize_unibet_matches(payload, sport="afl")
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key == {
        ("spread", "home -4.5"): 1.88,
        ("spread", "away 4.5"): 1.92,
        ("total", "over 165.5"): 1.9,
        ("total", "under 165.5"): 1.9,
    }


def test_grouped_wrappers_label_sports() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import (
        normalize_betr_all,
        normalize_unibet_all,
    )

    kambi_payload = {
        "sports": [
            {"sport": "australian_rules", "payload": {
                "events": [{"event": {"id": 1, "name": "A - B"},
                            "betOffers": [{"betOfferType": {"name": "Head to Head"}, "criterion": {},
                                           "outcomes": [{"type": "OT_ONE", "odds": 1500}]}]}]}},
            {"sport": "tennis", "payload": {
                "events": [{"event": {"id": 2, "name": "C - D"},
                            "betOffers": [{"betOfferType": {"name": "Head to Head"}, "criterion": {},
                                           "outcomes": [{"type": "OT_TWO", "odds": 2500}]}]}]}},
        ]
    }
    points = normalize_unibet_all(kambi_payload)
    assert {(p.sport, p.event_external_id) for p in points} == {("australian_rules", "1"), ("tennis", "2")}

    betr_payload = {"types": [{"sport": "australian_rules", "payload": {
        "MasterCategories": [{"Categories": [{"MasterEvents": [{
            "MasterEventId": 9, "MasterEventName": "X v Y",
            "Markets": [{"EventName": "Match Result (X v Y)", "OutcomeName": "X",
                         "Price": 1.5, "MarketDesc": "Win"}],
        }]}]}]}}]}
    points = normalize_betr_all(betr_payload)
    assert len(points) == 1 and points[0].sport == "australian_rules"
    assert normalize_unibet_all(None) == [] and normalize_betr_all([]) == []
