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
