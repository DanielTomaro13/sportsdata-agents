"""The watch registry is the customization contract: complete, typed, honest."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from sportsdata_agents.operations.monitoring import _in_quiet_hours
from sportsdata_agents.operations.watch_registry import (
    WATCH_PARAMS,
    params_for,
    parse_value,
    validate_params,
)

# The kinds run_watches dispatches — a registry entry must exist for every one,
# or `agents watches kinds` silently under-documents the platform.
DISPATCHED_KINDS = {
    "arb", "line_move", "steam", "value", "scratching", "model_value",
    "exchange_value", "stat_value", "racing_value", "prediction_value", "back_lay",
}


def test_registry_covers_every_dispatched_kind() -> None:
    assert set(WATCH_PARAMS) == DISPATCHED_KINDS


def test_params_for_merges_common_knobs() -> None:
    knobs = params_for("racing_value")
    assert "max_edge_pct" in knobs  # the artifact ceiling is user-tunable
    assert "quiet_hours" in knobs and "window_minutes" in knobs  # common set rides along


def test_validate_rejects_typos_with_the_valid_list() -> None:
    problems = validate_params("racing_value", {"minedge": 10})
    assert len(problems) == 1 and "min_edge_pct" in problems[0]
    assert validate_params("nope", {}) and "unknown watch kind" in validate_params("nope", {})[0]
    assert validate_params("arb", {"threshold_pct": 2.0, "quiet_hours": "23-08"}) == []


def test_parse_value_types() -> None:
    assert parse_value("8") == 8 and parse_value("0.75") == 0.75
    assert parse_value("true") is True and parse_value("off") is False
    assert parse_value("null") is None
    assert parse_value("FanDuel,BetMGM") == ["FanDuel", "BetMGM"]
    assert parse_value("Betfair") == "Betfair"
    assert parse_value("23-08") == "23-08"  # a quiet-hours spec stays a string


def _sub(**params: object) -> SimpleNamespace:
    return SimpleNamespace(params={"tz": "UTC", **params})


def test_quiet_hours_wraps_midnight() -> None:
    quiet = _sub(quiet_hours="23-08")
    assert _in_quiet_hours(quiet, dt.datetime(2026, 7, 7, 3, 0, tzinfo=dt.UTC))
    assert _in_quiet_hours(quiet, dt.datetime(2026, 7, 7, 23, 30, tzinfo=dt.UTC))
    assert not _in_quiet_hours(quiet, dt.datetime(2026, 7, 7, 12, 0, tzinfo=dt.UTC))


def test_quiet_hours_plain_window_and_defaults_off() -> None:
    daytime = _sub(quiet_hours="9-17")
    assert _in_quiet_hours(daytime, dt.datetime(2026, 7, 7, 12, 0, tzinfo=dt.UTC))
    assert not _in_quiet_hours(daytime, dt.datetime(2026, 7, 7, 20, 0, tzinfo=dt.UTC))
    assert not _in_quiet_hours(_sub(), dt.datetime(2026, 7, 7, 3, 0, tzinfo=dt.UTC))


def test_quiet_hours_malformed_specs_mean_no_silence() -> None:
    when = dt.datetime(2026, 7, 7, 3, 0, tzinfo=dt.UTC)
    for spec in ("nonsense", "25-08", "8-8", "a-b", "8"):
        assert not _in_quiet_hours(_sub(quiet_hours=spec), when), spec


def test_registry_defaults_match_the_monitoring_fallbacks() -> None:
    """Spot-check the drift seam: the registry documents what the code does."""
    racing = params_for("racing_value")
    assert racing["min_edge_pct"][0] == 8.0
    assert racing["max_edge_pct"][0] == 60.0
    assert racing["max_fair_odds"][0] == 12.0
    assert racing["min_matched"][0] == 500.0
    assert params_for("arb")["threshold_pct"][0] == 1.0
    assert params_for("steam")["min_moves"][0] == 3
    assert params_for("prediction_value")["min_volume"][0] == 100.0
