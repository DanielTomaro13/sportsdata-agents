"""The racing board hits the corporate books hard — and must never hit TAB hard.

The board polls live racecards, so it overrides the MCP engine's per-provider token
bucket to a high rate. That is fine for the corporate books: they are anonymous public
racecard feeds reached with no account behind them.

TAB is not like the others. It is the one source here reached through an authenticated
Akamai handshake on a real account, which makes hammering it an account risk rather than
a bandwidth question. So it is excluded by construction, and this file is what stops the
exclusion being lost to a later refactor that reaches for "everything except" or adds a
book to the list without noticing what it is.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_tab_is_never_in_the_unthrottled_set(monkeypatch) -> None:
    """THE guard. Even if someone explicitly names tab in the env, it is filtered out —
    the setting parses the list rather than trusting it, because the cost of getting this
    wrong lands on an account rather than on a retry."""
    monkeypatch.setenv("MF_UNTHROTTLED_BOOKS", "pointsbet,tab,sportsbet")
    import importlib

    from sportsdata_agents.interfaces.racingboard import config as cfg
    importlib.reload(cfg)
    try:
        assert "tab" not in cfg.Settings().unthrottled_books
        assert "pointsbet" in cfg.Settings().unthrottled_books
    finally:
        monkeypatch.delenv("MF_UNTHROTTLED_BOOKS", raising=False)
        importlib.reload(cfg)


def test_the_default_set_is_the_corporate_books_only() -> None:
    from sportsdata_agents.interfaces.racingboard.config import settings

    assert set(settings.unthrottled_books) == {"pointsbet", "sportsbet", "entain", "dabble"}
    assert settings.book_rps >= 10, "the board exists to poll fast; this is the whole point"


def test_the_override_reaches_the_engine_and_spares_tab() -> None:
    """The wiring, not just the setting: the board's OWN engine instance must carry the
    raised bucket for the books and TAB's spec limit for TAB. Without this the override
    is decorative."""
    from sportsdata_agents.interfaces.racingboard.config import settings
    from sportsdata_agents.interfaces.racingboard.engine import SportsDataEngine

    engine = SportsDataEngine()
    rates = {
        c._provider.id: c._bucket.rate
        for c in engine._clients
        if c._provider.id in set(settings.unthrottled_books) | {"tab"}
    }
    for book in settings.unthrottled_books:
        assert rates.get(book) == settings.book_rps, f"{book} did not get the raised rate"
    assert rates["tab"] < settings.book_rps, (
        "TAB was raised to the books' rate — it is the authenticated, Akamai-gated "
        "source and must keep its spec limit")
