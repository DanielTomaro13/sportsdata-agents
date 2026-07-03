"""Pricing-engine seam: resolution, degradation, and backend contracts."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from sportsdata_agents.quant.engines import (
    FOOTY_SPORTS,
    EnginePrice,
    EngineUnavailable,
    LocalEngineBackend,
    RemoteEngineBackend,
    resolve_engine,
)


def test_platform_runs_bare_by_default() -> None:
    assert resolve_engine() is None


def test_engine_price_fair_odds() -> None:
    assert EnginePrice("win", "A", 0.25).fair_odds == pytest.approx(4.0)
    assert EnginePrice("win", "A", 0.0).fair_odds == float("inf")


def test_remote_backend_requires_configuration() -> None:
    with pytest.raises(EngineUnavailable, match="ENGINE_API_URL"):
        RemoteEngineBackend("", "")
    backend = RemoteEngineBackend("https://engines.example", "key-123")
    with pytest.raises(EngineUnavailable, match="unreachable"):
        backend.sports()  # no service behind the URL: unavailable, never a crash


def _fake_engines_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal fake sportsdata_engines so the local backend is testable
    without the private package (this repo must never depend on it)."""

    class FakePrice:
        def __init__(self, market: str, selection: str, p: float) -> None:
            self.market, self.selection, self.fair_probability = market, selection, p
            self.line, self.std_error = None, None

    class FakeReport:
        def __init__(self) -> None:
            self.converged = True
            self.residuals: dict[str, float] = {}

    class FakeFixture:
        def __init__(self, sport: str, fixture_id: str, levers: Any = None, **kw: Any) -> None:
            self.sport, self.fixture_id = sport, fixture_id
            self.levers = dict(levers or {})

    core = types.ModuleType("sportsdata_engines.core")
    core.FixtureInputs = FakeFixture  # type: ignore[attr-defined]

    racing = types.ModuleType("sportsdata_engines.racing")
    racing.win_probabilities_from_odds = lambda odds: {  # type: ignore[attr-defined]
        r: (1 / o) / sum(1 / x for x in odds.values()) for r, o in odds.items()
    }
    racing.win_levers = lambda probs: {f"win:{r}": p for r, p in probs.items()}  # type: ignore[attr-defined]
    racing.price_board = lambda inputs: [  # type: ignore[attr-defined]
        FakePrice("win", r.removeprefix("win:"), p) for r, p in inputs.levers.items()
    ]

    afl = types.ModuleType("sportsdata_engines.afl")
    afl.anchors_from_quotes = lambda *a: ["anchors"]  # type: ignore[attr-defined]
    afl.fit_levers = lambda fixture: (  # type: ignore[attr-defined]
        {"expected_margin": 10.0, "expected_total": 170.0},
        FakeReport(),
    )
    afl.price_board = lambda fixture: [FakePrice("h2h", "home", 0.62)]  # type: ignore[attr-defined]

    root = types.ModuleType("sportsdata_engines")
    for name, module in {
        "sportsdata_engines": root,
        "sportsdata_engines.core": core,
        "sportsdata_engines.racing": racing,
        "sportsdata_engines.afl": afl,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_local_backend_prices_racing_and_footy(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_engines_modules(monkeypatch)
    backend = LocalEngineBackend()
    assert backend.sports() == ["racing", *FOOTY_SPORTS, "tennis"]

    board = backend.price_board("racing", "R1", {"win_odds": {"A": 2.0, "B": 2.0}})
    assert {p.selection for p in board} == {"A", "B"}
    assert board[0].fair_probability == pytest.approx(0.5)

    footy = backend.price_board("afl", "M1", {"h2h": [1.44, 2.81], "total": [186.5, 1.9, 1.9]})
    assert footy[0].market == "h2h" and footy[0].fair_probability == pytest.approx(0.62)

    with pytest.raises(ValueError, match="win_odds"):
        backend.price_board("racing", "R1", {})
    with pytest.raises(ValueError, match="footy quotes"):
        backend.price_board("afl", "M1", {"h2h": [1.9]})
    with pytest.raises(EngineUnavailable, match="does not price"):
        backend.price_board("cricket", "C1", {})


def test_consistency_scan_ranks_and_noise_gates() -> None:
    from sportsdata_agents.quant.engine_value import consistency_scan

    quotes = [
        {"market": "line", "selection": "home", "line": -12.5, "odds": 2.10},
        {"market": "total", "selection": "over", "line": 186.5, "odds": 1.95},
        {"market": "total", "selection": "under", "line": 186.5, "odds": 1.90},
        {"market": "h2h", "selection": "home", "odds": 1.44},  # no engine row: ignored
    ]
    engine = [
        {"market": "line", "selection": "home", "line": -12.5, "fair_probability": 0.52, "std_error": 0.003},
        {"market": "total", "selection": "over", "line": 186.5, "fair_probability": 0.53, "std_error": 0.003},
        # gap inside 3 std errors -> noise, even though edge clears the bar
        {"market": "total", "selection": "under", "line": 186.5, "fair_probability": 0.545, "std_error": 0.02},
    ]
    result = consistency_scan(quotes, engine, min_edge_pct=2.0, error_multiple=3.0)
    assert result["checked"] == 3
    assert result["skipped_noise"] == 1
    names = [(c["market"], c["selection"]) for c in result["candidates"]]
    assert names == [("line", "home"), ("total", "over")]  # sorted by edge desc
    assert result["candidates"][0]["edge_pct"] == pytest.approx((0.52 * 2.10 - 1) * 100, abs=0.01)

    with pytest.raises(ValueError, match=r"below 1\.01"):
        consistency_scan([{"market": "x", "selection": "s", "odds": 1.0}], engine)


def test_seam_reaches_the_breadth_sports(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_engines_modules(monkeypatch)
    backend = LocalEngineBackend()
    assert "soccer" in backend.sports() and "tennis" in backend.sports()
    assert len(backend.sports()) == 10
