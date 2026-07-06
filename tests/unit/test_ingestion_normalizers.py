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
           "pointsbet_all", "betr_all", "dabble_all", "fanduel_us", "fanduel_racing_win"}
    books = {"sportsbet_books", "tab_books", "unibet_books", "pinnacle_books", "pointsbet_books"}
    racing = {"tab_racing", "sportsbet_racing", "betr_racing", "pointsbet_racing", "unibet_racing"}
    futures = {"tab_racing_futures", "sportsbet_racing_futures",
               "pointsbet_racing_futures", "unibet_racing_futures"}
    prediction = {"kalshi_all", "polymarket_all"}
    assert set(FEEDS) == hot | books | racing | futures | prediction
    assert all(FEEDS[n].fetch is not None for n in hot | books | racing | futures | prediction)
    assert all(FEEDS[n].interval_s == 3600 for n in books)
    assert all(FEEDS[n].interval_s <= 300 for n in racing)  # racing moves near post
    assert all(FEEDS[n].interval_s == 3600 for n in futures)  # ante-post moves slowly
    assert all(FEEDS[n].interval_s == 900 for n in prediction)  # boards move on news


def test_rotation_window_derives_from_wall_clock(monkeypatch) -> None:
    """Cron `--once` runs are fresh processes — a process-lifetime offset would
    re-fetch the same first window forever (B2). The window advances with TIME."""
    import time as _time

    from sportsdata_agents.operations.ingestion import fetchers

    items = list(range(10))
    monkeypatch.setattr(_time, "time", lambda: 0.0)
    first = fetchers._take_rotating("t", items, 4)
    again = fetchers._take_rotating("t", items, 4)  # same epoch -> same window (restart-safe)
    assert first == again == [0, 1, 2, 3]
    monkeypatch.setattr(_time, "time", lambda: float(fetchers.ROTATION_EPOCH_S))
    assert fetchers._take_rotating("t", items, 4) == [4, 5, 6, 7]
    monkeypatch.setattr(_time, "time", lambda: float(2 * fetchers.ROTATION_EPOCH_S))
    assert fetchers._take_rotating("t", items, 4) == [8, 9, 0, 1]  # wraps
    assert fetchers._take_rotating("t", items, 12) == items  # under cap: everything


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


# ── AU-book racing (shapes captured live 2026-06-11) ──────────────────────


def test_normalize_tab_races_win_and_place() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_tab_races

    payload = {"races": [{
        "summary": {"raceNumber": 8, "raceStartTime": "2026-06-10T21:47:00.000Z",
                    "meeting": {"meetingDate": "2026-06-10", "raceType": "R",
                                "venueMnemonic": "HSE", "meetingName": "HORSESHOE INDIANAPOLIS"}},
        "card": {"runners": [
            {"runnerNumber": 1, "runnerName": "SEEN YOU LATER",
             "fixedOdds": {"returnWin": 81, "returnPlace": 6, "bettingStatus": "Open"}},
            {"runnerNumber": 2, "runnerName": "Scratchy",
             "fixedOdds": {"returnWin": 5, "bettingStatus": "Scratched"}},
        ]},
    }]}
    points = normalize_tab_races(payload)
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key == {("win", "1"): 81.0, ("place", "1"): 6.0}  # scratched skipped
    assert points[0].provider == "tab_racing" and points[0].sport == "horse_racing"
    assert points[0].event_external_id == "2026-06-10:R:HSE:R8"


def test_normalize_betr_races_market_codes() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_betr_races

    payload = {"races": [{"sport": "horse_racing", "card": {
        "EventId": 88112233, "EventName": "Sale R1", "AdvertisedStartTime": "2026-06-11T02:00:00Z",
        "Outcomes": [{"OutcomeId": 1, "OutcomeName": "Seen You Later", "FixedPrices": [
            {"MarketTypeCode": "WIN", "Price": 51.0}, {"MarketTypeCode": "PLC", "Price": 4.6}]}],
    }}]}
    points = normalize_betr_races(payload)
    assert {(p.market, p.selection, p.odds) for p in points} == {("win", "1", 51.0), ("place", "1", 4.6)}
    assert points[0].provider == "betr_racing"


def test_normalize_pointsbet_races_fluctuations() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_pointsbet_races

    payload = {"races": [{
        "raceId": "110085943", "venue": "Sale", "number": 1, "racingType": "Thoroughbred",
        "runners": [
            {"number": 1, "runnerName": "Bank Heist", "isScratched": False,
             "fluctuations": {"open": 9.5, "current": 8.5}},
            {"number": 2, "runnerName": "Gone", "isScratched": True,
             "fluctuations": {"current": 3.0}},
        ],
    }]}
    points = normalize_pointsbet_races(payload)
    assert [(p.market, p.selection, p.odds) for p in points] == [("win", "1", 8.5)]
    assert points[0].sport == "horse_racing" and points[0].event_name == "Sale R1"


def test_normalize_unibet_races_current_fluc() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_unibet_races

    payload = {"races": [{
        "sport": "horse_racing", "eventKey": "202606110400.T.AUS.casino.4",
        "card": {"data": {"viewer": {"event": {
            "name": "Thomas Noble & Russell (Bm66)",
            "competitors": [{
                "name": "Raging Pixie", "number": 7,
                "prices": [
                    {"betType": "FixedWin", "flucs": [
                        {"price": 12, "productType": "Current"}, {"price": 13, "productType": "Max"}]},
                    {"betType": "FixedPlace", "flucs": [{"price": 3.2, "productType": "Current"}]},
                ],
            }],
        }}}},
    }]}
    points = normalize_unibet_races(payload)
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key == {("win", "7"): 12.0, ("place", "7"): 3.2}
    assert points[0].provider == "unibet_racing"


def test_normalize_sportsbet_races_uses_market_parser() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_sportsbet_races

    payload = {
        "sports": {"10575804": "horse_racing"},
        "meetings": {"10575804": "Seymour"},
        "events": [{
            "id": 10575804, "raceNumber": 1, "startTime": 1781146800,
            "markets": [{"name": "Win or Place", "selections": [
                {"name": "Fast Horse", "runnerNumber": 3, "price": {"winPrice": 4.2}},
                {"name": "No Price", "runnerNumber": 4, "price": {}},
            ]}],
        }],
    }
    points = normalize_sportsbet_races(payload)
    assert [(p.market, p.selection, p.odds) for p in points] == [("win", "3", 4.2)]
    assert points[0].event_name == "Seymour R1" and points[0].provider == "sportsbet_racing"


def test_sportsbet_races_reads_prices_array_fixed_odds() -> None:
    """Racecard selections price via a prices[] array; fixed entries carry winPrice,
    tote-only entries (MID/MDP) don't — those races yield nothing until fixed opens."""
    from sportsdata_agents.operations.ingestion.normalizers import normalize_sportsbet_races

    payload = {
        "sports": {"7": "horse_racing"}, "meetings": {"7": "Seymour"},
        "events": [{
            "id": 7, "raceNumber": 2,
            "markets": [{"name": "Win or Place", "selections": [
                {"name": "Fixed Runner", "runnerNumber": 5,
                 "prices": [{"priceCode": "L", "winPrice": 6.5}, {"priceCode": "MID"}]},
                {"name": "Tote Only", "runnerNumber": 6, "prices": [{"priceCode": "MID"}]},
            ]}],
        }],
    }
    points = normalize_sportsbet_races(payload)
    assert [(p.market, p.selection, p.odds) for p in points] == [("win", "5", 6.5)]


# ── futures (shapes captured live 2026-06-11) ──────────────────────────────


def test_tab_racing_futures_name_keyed_events() -> None:
    """Futures cards have raceNumber 0 and unnumbered runners — the race NAME keys
    the event and the horse NAME keys the selection, or every Cup market collides."""
    from sportsdata_agents.operations.ingestion.normalizers import normalize_tab_races

    payload = {"races": [{
        "summary": {"raceNumber": 0, "raceName": "Queen Anne Stakes (All In)",
                    "raceStartTime": "2026-06-16T06:00:00.000Z",
                    "meeting": {"meetingDate": "2026-06-16", "raceType": "R",
                                "venueMnemonic": "Racing Futures",
                                "meetingName": "Racing Futures"}},
        "card": {"runners": [
            {"runnerNumber": None, "runnerName": "NOTABLE SPEECH",
             "fixedOdds": {"returnWin": 2.5, "returnPlace": 1.3, "bettingStatus": "Open"}},
        ]},
    }]}
    points = normalize_tab_races(payload)
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key == {("win", "notable speech"): 2.5, ("place", "notable speech"): 1.3}
    assert points[0].event_external_id == "2026-06-16:R:Racing Futures:Queen Anne Stakes (All In)"
    assert points[0].event_name == "Racing Futures Queen Anne Stakes (All In)"
    assert points[0].meta["post_time"] == "2026-06-16T06:00:00.000Z"


def test_unibet_antepost_prices_without_flucs() -> None:
    """Ante-post competitor prices carry NO flucs — the row's direct price is live."""
    from sportsdata_agents.operations.ingestion.normalizers import normalize_unibet_races

    payload = {"races": [{
        "sport": "horse_racing", "eventKey": "202606161300.T.GBR.antepost___royal_ascot.1",
        "card": {"data": {"viewer": {"event": {
            "name": "Queen Anne Stakes", "eventDateTimeUtc": "2026-06-16T13:30:00Z",
            "competitors": [{
                "name": "Notable Speech", "number": None,
                "prices": [
                    {"betType": "FixedWin", "price": 4.5, "flucs": []},
                    {"betType": "FixedPlace", "price": 1.24, "flucs": []},
                ],
            }],
        }}}},
    }]}
    points = normalize_unibet_races(payload)
    by_key = {(p.market, p.selection): p.odds for p in points}
    assert by_key == {("win", "notable speech"): 4.5, ("place", "notable speech"): 1.24}
    assert points[0].meta["post_time"] == "2026-06-16T13:30:00Z"


def test_pinnacle_outright_named_from_special_or_league() -> None:
    """Outright matchups have no home/away alignments — the event name must come
    from the special description or league, never '? v ?' (B12)."""
    from sportsdata_agents.operations.ingestion.normalizers import normalize_pinnacle_league

    payload = {
        "matchups": [{
            "id": 99, "_sport": "australian_rules",
            "league": {"name": "AFL 2026"},
            "special": {"description": "Brownlow Medal Winner"},
            "participants": [{"id": 1, "name": "Marcus Bontempelli"}],
            "startTime": "2026-09-20T09:00:00Z",
        }],
        "markets": {"99": [{
            "type": "moneyline", "status": "open",
            "prices": [{"participantId": 1, "price": 350}],
        }]},
    }
    points = normalize_pinnacle_league(payload, sport="?")
    assert points and points[0].event_name == "Brownlow Medal Winner"
    assert points[0].selection == "marcus bontempelli"


def test_sportsbet_outrights_flow_through_matches_normalizer() -> None:
    """competition_outrights returns the SAME grouped shape as competition_matches —
    one normalizer serves both routes (B10)."""
    from sportsdata_agents.operations.ingestion.normalizers import normalize_sportsbet_matches

    payload = [{
        "groupName": "2026 AFL Brownlow Medal",
        "events": [{
            "id": 9641792, "displayName": "2026 AFL Brownlow Medal",
            "bettingStatus": "PRICED", "startTime": 1789983000,
            "primaryMarket": {
                "name": "2026 AFL Brownlow Medal", "marketSort": "--",
                "selections": [
                    {"name": "Marcus Bontempelli", "resultType": "-", "price": {"winPrice": 4.5}},
                ],
            },
        }],
    }]
    points = normalize_sportsbet_matches(payload, sport="australian_rules")
    assert [(p.market, p.selection, p.odds) for p in points] == [
        ("2026 afl brownlow medal", "marcus bontempelli", 4.5)
    ]
    assert points[0].event_name == "2026 AFL Brownlow Medal"


def test_discover_fanduel_pages_parses_navigation_slugs() -> None:
    """The nav scaffolding's /navigation/{slug} links ARE the content-page ids (B8)."""
    import asyncio

    from sportsdata_agents.operations.ingestion.fetchers import discover_fanduel_pages

    class FakeManager:
        async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
            assert name == "fanduel_sb_call"
            return {"QUICK_LINKS": [
                {"url": "https://sportsbook.fanduel.com/navigation/nba"},
                {"url": "https://sportsbook.fanduel.com/navigation/pga"},
                {"url": "https://sportsbook.fanduel.com/navigation/nba"},  # dupe
                {"url": "https://sportsbook.fanduel.com/promotions"},  # not a page
            ]}

    slugs = asyncio.run(discover_fanduel_pages(FakeManager()))
    assert slugs == ["nba", "pga"]


def test_feeds_due_in_window_paces_each_interval() -> None:
    """Stateless cron pacing: `--cron N` every N seconds runs fast feeds every
    tick and slow feeds only on the tick that crosses their interval boundary."""
    from sportsdata_agents.operations.ingestion import Feed, feeds_due_in_window
    from sportsdata_agents.operations.ingestion.normalizers import normalize_nba_odds

    fast = Feed(name="fast", tool="t", mcp_groups=("g",), normalizer=normalize_nba_odds,
                interval_s=180)
    slow = Feed(name="slow", tool="t", mcp_groups=("g",), normalizer=normalize_nba_odds,
                interval_s=3600)
    feeds = [fast, slow]
    # tick at 10:03 (no hour boundary in the last 180s): only the fast feed
    due = feeds_due_in_window(feeds, now_s=3600 * 10 + 180, period_s=180)
    assert [f.name for f in due] == ["fast"]
    # tick at 11:00:00 (hour boundary crossed): both
    due = feeds_due_in_window(feeds, now_s=3600 * 11, period_s=180)
    assert [f.name for f in due] == ["fast", "slow"]


# ── prediction markets: Kalshi / Polymarket (shapes per the v0.4.0 specs) ──

KALSHI_PAYLOAD = {
    "pages": [
        {
            "cursor": "abc",
            "events": [
                {
                    "event_ticker": "KXNBAGAME-26JUN11OKCIND",
                    "series_ticker": "KXNBAGAME",
                    "title": "Thunder vs Pacers: Game 4 Winner?",
                    "category": "Sports",
                    "mutually_exclusive": True,
                    "markets": [
                        {
                            "ticker": "KXNBAGAME-26JUN11OKCIND-OKC",
                            "yes_sub_title": "Thunder",
                            "status": "open",
                            "yes_ask_dollars": "0.55",
                            "no_ask_dollars": "0.47",
                            "expected_expiration_time": "2026-06-12T01:30:00Z",
                            "volume_24h_fp": "120000",
                            "open_interest_fp": "60000",
                            "close_time": "2026-06-12T02:00:00Z",
                        },
                        {
                            "ticker": "KXNBAGAME-26JUN11OKCIND-IND",
                            "yes_sub_title": "Pacers",
                            "status": "open",
                            "yes_ask": 47,  # cents fallback (no dollars field)
                            "no_ask": 57,
                        },
                        {  # settled contracts carry no live book
                            "ticker": "KXNBAGAME-26JUN11OKCIND-VOID",
                            "yes_sub_title": "Void",
                            "status": "settled",
                            "yes_ask_dollars": "0.99",
                        },
                    ],
                },
                {"title": "No ticker — skipped", "markets": []},
            ],
        }
    ]
}


def test_normalize_kalshi_events() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_kalshi_all

    points = normalize_kalshi_all(KALSHI_PAYLOAD)
    by_sel = {p.selection: p for p in points}
    # YES and NO per open contract; the settled one stays out
    assert set(by_sel) == {"thunder", "no thunder", "pacers", "no pacers"}
    thunder = by_sel["thunder"]
    # the GAME series ticker names the league -> the cross-book sport family
    assert (thunder.provider, thunder.book, thunder.sport) == ("kalshi", "Kalshi", "basketball")
    assert thunder.event_external_id == "KXNBAGAME-26JUN11OKCIND"
    # the title's "X vs Y" core becomes the event name (resolver-splittable);
    # the trailing ": Game 4 Winner?" qualifier is stripped
    assert thunder.event_name == "Thunder vs Pacers"
    # the seed dictionary already families the verified game series onto h2h
    assert thunder.market == "h2h"
    assert thunder.meta["end_time"] == "2026-06-12T01:30:00Z"  # expected expiration ≈ game END
    assert "start_time" not in thunder.meta  # never a fake start (would fool the arb in-play gate)
    assert thunder.odds == pytest.approx(1 / 0.55, abs=1e-3)
    assert by_sel["pacers"].odds == pytest.approx(1 / 0.47, abs=1e-3)  # cents path
    assert thunder.meta["close_time"] == "2026-06-12T02:00:00Z"
    assert normalize_kalshi_all({}) == []
    assert normalize_kalshi_all([]) == []


def test_kalshi_event_name_extracts_the_matchup() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import _kalshi_event_name

    # live-captured title shapes (2026-06-11)
    assert _kalshi_event_name("Game 5: New York at San Antonio") == "New York vs San Antonio"
    assert _kalshi_event_name("Thunder vs Pacers: Game 4 Winner?") == "Thunder vs Pacers"
    assert _kalshi_event_name("Liverpool v Everton") == "Liverpool vs Everton"
    assert _kalshi_event_name("Pacers at Thunder Winner?") == "Pacers vs Thunder"
    # non-matchup titles pass through untouched — never mangled
    assert _kalshi_event_name("Matthew Stafford: Retirement") == "Matthew Stafford: Retirement"
    assert _kalshi_event_name("New York J: To Break Playoff Drought") == (
        "New York J: To Break Playoff Drought")


POLYMARKET_PAYLOAD = {
    "pages": [
        [
            {
                "id": "903193",
                "slug": "nba-champion-2026",
                "title": "NBA Champion 2026",
                "tags": [{"label": "Sports"}, {"label": "NBA"}],
                "markets": [
                    {
                        "id": "514501",
                        "question": "Will the Thunder win the 2026 NBA Finals?",
                        "groupItemTitle": "Thunder",
                        "sportsMarketType": "moneyline",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.62", "0.38"]',
                        "volume24hr": 250000.5,
                        "endDate": "2026-06-22T00:00:00Z",
                    },
                    {
                        "id": "514502",
                        "question": "Will the Pacers win the 2026 NBA Finals?",
                        "groupItemTitle": "Pacers",
                        "outcomes": ["Yes", "No"],  # already-decoded lists pass too
                        "outcomePrices": ["0.38", "0.62"],
                    },
                    {
                        "id": "514503",
                        "question": "Closed market",
                        "closed": True,
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.5", "0.5"]',
                    },
                ],
            },
            {
                "id": "903500",
                "title": "Will it rain in NYC tomorrow?",
                "tags": [],
                "markets": [
                    {
                        "id": "600100",
                        "question": "Will it rain in NYC tomorrow?",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.2", "0.8"]',
                    }
                ],
            },
        ]
    ]
}


def test_normalize_polymarket_events() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_polymarket_all

    points = normalize_polymarket_all(POLYMARKET_PAYLOAD)
    grouped = {p.selection: p for p in points if p.event_external_id == "903193"}
    # grouped markets: the subject is the selection (yes side), "no <subject>" rides along
    assert set(grouped) == {"thunder", "no thunder", "pacers", "no pacers"}
    thunder = grouped["thunder"]
    assert (thunder.provider, thunder.book) == ("polymarket", "Polymarket")
    # the specific tag wins over the portal "Sports" tag, then the dictionary
    # folds the league label onto the cross-book sport family
    assert thunder.sport == "basketball"
    assert thunder.event_name == "NBA Champion 2026"
    assert thunder.market == "h2h"  # Gamma's sportsMarketType rides the dictionary
    assert thunder.meta["end_time"] == "2026-06-22T00:00:00Z"  # endDate as the day-window proxy (END, not start)
    assert thunder.odds == pytest.approx(1 / 0.62, abs=1e-3)
    # no sportsMarketType -> the event title is the (book-local) market key
    assert grouped["pacers"].market == "nba champion 2026"
    assert grouped["no thunder"].odds == pytest.approx(1 / 0.38, abs=1e-3)
    binary = {p.selection: p for p in points if p.event_external_id == "903500"}
    assert set(binary) == {"yes", "no"}  # plain binary: the outcomes ARE the selections
    assert binary["yes"].odds == pytest.approx(5.0, abs=1e-3)
    assert normalize_polymarket_all({}) == []
    assert normalize_polymarket_all([]) == []


DABBLE_PAYLOAD = {
    "competitions": [
        {
            "sport": "Australian Rules",
            "competition": "AFL Matches",
            "fixtures": [
                {   # listing row for the SAME fixture as the detail below —
                    # the detail's fuller board must win in the sink
                    "id": "fx-1",
                    "name": "Fremantle Dockers v Sydney Swans",
                    "advertisedStart": "2026-07-09T10:10:00.000Z",
                    "markets": [{"id": "m-h2h", "name": "Match Winner", "isDisplayed": True}],
                    "selections": [{"id": "s-freo", "name": "Fremantle", "isDisplayed": True}],
                    "prices": [{"marketId": "m-h2h", "selectionId": "s-freo", "price": 1.50}],
                },
                {   # listing-only fixture (no detail fetched this cycle)
                    "id": "fx-2",
                    "name": "Carlton v Collingwood",
                    "markets": [{"id": "m2", "name": "Match Winner"}],
                    "selections": [{"id": "s2", "name": "Carlton"}],
                    "prices": [{"marketId": "m2", "selectionId": "s2", "price": 2.10}],
                },
            ],
            "details": [
                {
                    "id": "fx-1",
                    "name": "Fremantle Dockers v Sydney Swans",
                    "advertisedStart": "2026-07-09T10:10:00.000Z",
                    "markets": [
                        {"id": "m-h2h", "name": "Match Winner", "isDisplayed": True},
                        {"id": "m-q1", "name": "First Quarter Winner", "isDisplayed": True},
                        {"id": "m-hidden", "name": "Ghost Market", "isDisplayed": False},
                        {"id": "m-prop", "name": "Player Disposals O/U (24.5)", "isDisplayed": True},
                    ],
                    "selections": [
                        {"id": "s-freo", "name": "Fremantle", "isDisplayed": True},
                        {"id": "s-syd", "name": "Sydney", "isDisplayed": True},
                        {"id": "s-scr", "name": "Scratched Guy", "isScratched": True},
                        {"id": "s-over", "name": "Over", "isDisplayed": True},
                    ],
                    "prices": [
                        {"marketId": "m-h2h", "selectionId": "s-freo", "price": 1.46},
                        {"marketId": "m-q1", "selectionId": "s-syd", "price": 2.30},
                        {"marketId": "m-hidden", "selectionId": "s-freo", "price": 3.0},
                        {"marketId": "m-h2h", "selectionId": "s-scr", "price": 9.0},
                        {"marketId": "m-prop", "selectionId": "s-over", "price": 1.87},
                        {"marketId": "m-h2h", "selectionId": "s-freo", "price": 0.5},
                    ],
                    "playerProps": [
                        {"playerId": "p1", "playerName": "Nat Fyfe", "teamName": "Fremantle",
                         "selectionId": "s-over", "marketId": "m-prop",
                         "stats": ["disposals"], "value": 24.5, "lineType": "over"},
                    ],
                }
            ],
        }
    ]
}


def test_normalize_dabble_all() -> None:
    from sportsdata_agents.operations.ingestion.normalizers import normalize_dabble_all

    points = normalize_dabble_all(DABBLE_PAYLOAD)
    by_key = {(p.event_external_id, p.market, p.selection): p for p in points}
    # detail normalizes first: its h2h price (1.46) beats the listing's 1.50
    freo = by_key[("fx-1", "h2h", "home")]
    assert (freo.provider, freo.book, freo.odds) == ("dabble", "Dabble", 1.46)
    assert freo.sport == "australian_rules"
    assert freo.meta["competition"] == "AFL Matches"
    # quarter derivative captured with the away side resolved from "X v Y"
    assert by_key[("fx-1", "first quarter winner", "away")].odds == 2.30
    # hidden market and scratched selection are skipped
    assert not any(p.market == "ghost market" for p in points)
    assert not any(p.selection == "scratched guy" for p in points)
    # Pick'em playerProps join enriches the priced selection's meta
    prop = by_key[("fx-1", "player disposals o/u (24.5)", "over")]
    assert prop.meta["player"] == "Nat Fyfe"
    assert prop.meta["stat"] == "disposals"
    assert prop.meta["stat_line"] == 24.5
    assert prop.meta["line_type"] == "over"
    # listing-only fixture still captures
    assert by_key[("fx-2", "h2h", "home")].odds == 2.10
    assert normalize_dabble_all({}) == []
    assert normalize_dabble_all([]) == []
