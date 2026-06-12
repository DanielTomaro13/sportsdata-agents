"""Operator console: spend report + budget, and the config preflight."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from sportsdata_agents.operations import costs, preflight

pytestmark = pytest.mark.unit


def test_budget_set_get_and_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    assert costs.get_budget() is None
    b = costs.set_budget(50, "monthly")
    assert b == {"period": "monthly", "cap_usd": 50.0}
    assert costs.get_budget() == b
    with pytest.raises(ValueError, match="period must be"):
        costs.set_budget(10, "yearly")
    with pytest.raises(ValueError, match="cap must be"):
        costs.set_budget(-1)


def test_period_start_boundaries() -> None:
    now = dt.datetime(2026, 6, 17, 14, 30, tzinfo=dt.UTC)  # a Wednesday
    assert costs._period_start("daily", now).day == 17
    assert costs._period_start("weekly", now).day == 15  # Monday
    assert costs._period_start("monthly", now).day == 1


def test_preflight_reports_core_and_commercial(monkeypatch: pytest.MonkeyPatch) -> None:
    # a clean env: no provider key, no commercial setup
    for var in ("SPORTSDATA_LICENSE_PRIVKEY", "SPORTSDATA_BILLING_PRODUCTS", "PADDLE_WEBHOOK_SECRET",
                "LEMONSQUEEZY_WEBHOOK_SECRET", "SMTP_HOST", "SPORTSDATA_OPERATOR",
                "SPORTSDATA_GATEWAY_TOKEN", "SPORTSDATA_LICENSE_PUBKEY"):
        monkeypatch.delenv(var, raising=False)
    import sportsdata_agents.app.wizard as wizard
    monkeypatch.setattr(wizard, "configured_provider", lambda: None)

    checks = {c.label: c for c in preflight.run_preflight()}
    assert checks["Model provider"].status == "missing"
    assert checks["Signing private key"].status == "missing"  # commercial not set up
    assert checks["Operator mode"].status == "info"           # customer install
    summary = preflight.summarise(list(checks.values()))
    assert summary["missing"] >= 1 and summary["ok"] >= 1     # warehouse is always ok


def test_preflight_operator_mode_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_OPERATOR", "1")
    import sportsdata_agents.app.wizard as wizard
    monkeypatch.setattr(wizard, "configured_provider", lambda: None)
    checks = {c.label: c for c in preflight.run_preflight()}
    assert checks["Operator mode"].status == "ok"
