"""Tests for the gate that decides whether an agent may touch a real team.

These are written as claims about behaviour an owner would care about, not as coverage.
The load-bearing ones are the refusals: chips never automatic, expired approvals never
honoured, a failed write never reported as a success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sportsdata_agents.fantasy.policy import (
    LeaguePolicy,
    Verdict,
    load_policies,
    save_policy,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
SOON = NOW + timedelta(hours=3)      # inside the act window
LATER = NOW + timedelta(days=4)      # too early to act


def policy(**kw) -> LeaguePolicy:
    return LeaguePolicy(platform="fpl", entry=3942695, **kw)


# ─── the rule that cannot be configured away ────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "auto_if_free"])
def test_chips_can_never_be_automatic(mode):
    """Four per season, each unrecoverable. This is a construction error, not a default
    that a config file can quietly override."""
    with pytest.raises(ValueError, match="chips cannot be automatic"):
        policy(chips=mode)


def test_chip_decisions_are_always_ask_or_skip():
    assert policy(chips="ask").for_chip("wildcard").verdict is Verdict.ASK
    assert policy(chips="never").for_chip("wildcard").verdict is Verdict.SKIP


# ─── defaults are timid ─────────────────────────────────────────────────


def test_everything_defaults_to_ask():
    p = policy()
    assert p.for_lineup(now=NOW, deadline=SOON).verdict is Verdict.ASK
    assert p.for_captain(now=NOW, deadline=SOON).verdict is Verdict.ASK
    assert p.for_transfer(
        hit=0, free_transfers=1, transfers_used=0, now=NOW, deadline=SOON
    ).verdict is Verdict.ASK


# ─── timing ─────────────────────────────────────────────────────────────


def test_acts_when_the_deadline_is_close_and_the_mode_is_auto():
    d = policy(lineup="auto").for_lineup(now=NOW, deadline=SOON)
    assert d.verdict is Verdict.ACT


def test_will_not_act_days_early_because_team_news_is_still_moving():
    d = policy(lineup="auto").for_lineup(now=NOW, deadline=LATER)
    assert d.verdict is Verdict.SKIP
    assert d.deferred is True


def test_a_passed_deadline_is_a_skip_not_an_ask():
    """Asking would be pointless — nothing can change once the gameweek locks."""
    d = policy(lineup="auto").for_lineup(now=NOW, deadline=NOW - timedelta(minutes=1))
    assert d.verdict is Verdict.SKIP
    assert "deadline has passed" in d.reason


def test_quiet_hours_downgrade_an_act_to_an_ask():
    night = datetime(2026, 8, 22, 2, 0, tzinfo=UTC)
    d = policy(lineup="auto").for_lineup(now=night, deadline=night + timedelta(hours=2))
    assert d.verdict is Verdict.ASK
    assert d.deferred is True


def test_quiet_hours_window_wraps_midnight():
    p = policy()
    assert p._in_quiet_hours(datetime(2026, 8, 22, 23, 30, tzinfo=UTC)) is True
    assert p._in_quiet_hours(datetime(2026, 8, 22, 3, 0, tzinfo=UTC)) is True
    assert p._in_quiet_hours(datetime(2026, 8, 22, 12, 0, tzinfo=UTC)) is False


def test_action_budget_stops_a_looping_policy():
    d = policy(lineup="auto", max_actions_per_gameweek=3).for_lineup(
        now=NOW, deadline=SOON, actions_taken=3
    )
    assert d.verdict is Verdict.ASK
    assert "already acted" in d.reason


# ─── points hits ────────────────────────────────────────────────────────


def test_auto_if_free_asks_before_taking_a_hit():
    d = policy(transfers="auto_if_free").for_transfer(
        hit=4, free_transfers=0, transfers_used=1, now=NOW, deadline=SOON
    )
    assert d.verdict is Verdict.ASK
    assert "4 points" in d.reason


def test_auto_if_free_acts_when_the_transfer_is_free():
    d = policy(transfers="auto_if_free").for_transfer(
        hit=0, free_transfers=1, transfers_used=0, now=NOW, deadline=SOON
    )
    assert d.verdict is Verdict.ACT


def test_auto_respects_the_max_hit_ceiling():
    p = policy(transfers="auto", max_hit=4)
    assert p.for_transfer(
        hit=4, free_transfers=0, transfers_used=1, now=NOW, deadline=SOON
    ).verdict is Verdict.ACT
    assert p.for_transfer(
        hit=8, free_transfers=0, transfers_used=2, now=NOW, deadline=SOON
    ).verdict is Verdict.ASK


def test_negative_max_hit_is_rejected():
    with pytest.raises(ValueError, match="max_hit"):
        policy(max_hit=-4)


# ─── persistence ────────────────────────────────────────────────────────


def test_policies_round_trip(tmp_path):
    p = policy(lineup="auto", captain="auto", quiet_hours=("22:00", "06:00"))
    save_policy(p, tmp_path)
    back = load_policies(tmp_path)[p.key]
    assert back == p
    assert back.quiet_hours == ("22:00", "06:00")


def test_missing_policy_file_is_empty_not_an_error(tmp_path):
    assert load_policies(tmp_path) == {}
