"""Phase A — the one path from an intent to money leaving an account.

The whole point of `run_intent` is that six things happen in the same order every time.
These tests exist to make each of them fail loudly if it is ever reordered or skipped,
because every one of them was learned from a book's real behaviour rather than deduced.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sportsdata_agents.betting import drift
from sportsdata_agents.betting.execute import Intent, _verdict_of, run_intent
from sportsdata_agents.betting.ledger import Ledger
from sportsdata_agents.betting.policy import BettingPolicy

pytestmark = pytest.mark.unit

NOON = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class Calls:
    """A fake tool caller that records everything and can be told how to answer."""

    def __init__(self, answers: dict | None = None, fail: str | None = None) -> None:
        self.answers = answers or {}
        self.fail = fail
        self.seen: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, /, **kwargs):
        self.seen.append((tool, kwargs))
        if self.fail and tool == self.fail:
            raise TimeoutError("upstream went away")
        return self.answers.get(tool, {})

    def names(self) -> list[str]:
        return [t for t, _ in self.seen]


def intent(**kw) -> Intent:
    base = {
        "book": "sportsbet",
        "legs": [{"sel": "Bulldogs"}],
        "odds": 3.0,
        "edge": 0.10,
        "payload": {"betItems": [{"betNo": 1}]},
    }
    base.update(kw)
    return Intent(**base)  # type: ignore[arg-type]


def auto_policy(**kw) -> BettingPolicy:
    return BettingPolicy(book_modes={"sportsbet": "auto"}, books=["sportsbet"], **kw)


# ─── 1 & 2: policy decides first, and stops what it does not clear ──────


@pytest.mark.asyncio
async def test_paper_mode_never_calls_a_bookmaker(tmp_path) -> None:
    calls = Calls()
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(intent(), policy=BettingPolicy(), ledger=ledger, call=calls, now=NOON)
    assert out.status == "paper"
    assert calls.names() == []          # the decisive assertion
    assert [e.status for e in ledger] == ["paper"]


@pytest.mark.asyncio
async def test_a_skip_touches_nothing(tmp_path) -> None:
    calls = Calls()
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(intent(edge=0.001), policy=auto_policy(), ledger=ledger, call=calls, now=NOON)
    assert out.status == "skipped" and calls.names() == []


@pytest.mark.asyncio
async def test_ask_mode_notifies_and_stops(tmp_path) -> None:
    calls = Calls()
    ledger = Ledger(tmp_path / "l.jsonl")
    asked: list = []

    async def on_ask(i, d):
        asked.append((i, d))

    policy = BettingPolicy(book_modes={"sportsbet": "ask"}, books=["sportsbet"])
    out = await run_intent(intent(), policy=policy, ledger=ledger, call=calls, now=NOON, on_ask=on_ask)
    assert out.status == "asked"
    assert calls.names() == []
    assert len(asked) == 1


# ─── 3: the drift gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_shortened_price_abandons_the_bet(tmp_path) -> None:
    """The edge was the entire reason for the bet; if the price moved against us past
    tolerance, placing anyway is placing a different, worse bet."""
    calls = Calls(answers={"sportsbet_price_slip": {"price": 2.5}})
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(
        intent(reprice_args={"betItems": []}),
        policy=auto_policy(max_price_drift=0.02),
        ledger=ledger, call=calls, now=NOON,
        reprice=lambda q: q["price"],
    )
    assert out.status == "rejected"
    assert "shortened" in out.reason
    assert "sportsbet_place_bet" not in calls.names()   # never reached the money call


@pytest.mark.asyncio
async def test_a_drifted_out_price_still_places(tmp_path) -> None:
    """One-sided on purpose: movement in our favour grew the edge, and abandoning there
    would quietly discard the best opportunities."""
    calls = Calls(answers={
        "sportsbet_price_slip": {"price": 4.0},
        "sportsbet_place_bet": {"betId": "B1"},
    })
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(
        intent(reprice_args={"betItems": []}),
        policy=auto_policy(), ledger=ledger, call=calls, now=NOON,
        reprice=lambda q: q["price"],
    )
    assert out.status == "placed"
    assert out.price == 4.0     # placed at the book's number, not the remembered one


@pytest.mark.asyncio
async def test_a_failed_reprice_refuses_to_place_blind(tmp_path) -> None:
    calls = Calls(answers={}, fail="sportsbet_price_slip")
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(
        intent(reprice_args={"betItems": []}),
        policy=auto_policy(), ledger=ledger, call=calls, now=NOON,
        reprice=lambda q: q["price"],
    )
    assert out.status == "rejected"
    assert "sportsbet_place_bet" not in calls.names()


# ─── 4: placed once, never retried ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_placement_is_called_exactly_once(tmp_path) -> None:
    calls = Calls(answers={"sportsbet_place_bet": {"betId": "B1"}})
    ledger = Ledger(tmp_path / "l.jsonl")
    await run_intent(intent(), policy=auto_policy(), ledger=ledger, call=calls, now=NOON)
    assert calls.names().count("sportsbet_place_bet") == 1


@pytest.mark.asyncio
async def test_a_timed_out_placement_is_not_resent(tmp_path) -> None:
    """None of Sportsbet, Entain or Unibet gives a usable idempotency key — their
    receipts are returned, not sent — so a resend is a SECOND BET, not a retry."""
    calls = Calls(fail="sportsbet_place_bet")
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(intent(), policy=auto_policy(), ledger=ledger, call=calls, now=NOON)
    assert calls.names().count("sportsbet_place_bet") == 1
    assert out.status == "rejected"
    assert "NOT retried" in out.reason and "verify by reading the account" in out.reason


# ─── 5: the body is the verdict, not the status code ───────────────────


def test_entain_puts_the_verdict_in_the_body_not_the_status_code() -> None:
    """Entain answers 200 and says what happened in `status`. Anything other than
    'accepted' is a bet that did not go on, whatever the HTTP code said."""
    ok, reason, _ = _verdict_of("entain", {"status": "rejected", "message": "price changed"})
    assert not ok and "rejected" in reason
    assert _verdict_of("entain", {"status": "accepted"})[0]


def test_kambi_rejection_envelope_is_read_as_a_refusal() -> None:
    """Unibet's success body was never observed, so a `{status, message}` rejection
    envelope is the reliable signal and anything else is reported as unconfirmed."""
    ok, _, _ = _verdict_of("unibet", {"status": 400, "message": "coupon is stale"})
    assert not ok
    ok, reason, _ = _verdict_of("unibet", {"couponRows": [{}]})
    assert ok and "unpinned" in reason


def test_an_unknown_book_is_never_reported_as_placed() -> None:
    assert not _verdict_of("someothercorp", {"anything": True})[0]


@pytest.mark.asyncio
async def test_sportsbet_without_a_betid_is_not_a_placement(tmp_path) -> None:
    """202 Accepted means taken for processing. The betId is the receipt, and without
    one there is nothing to confirm against later."""
    calls = Calls(answers={"sportsbet_place_bet": {}})
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(intent(), policy=auto_policy(), ledger=ledger, call=calls, now=NOON)
    assert out.status == "rejected" and "no betId" in out.reason


@pytest.mark.asyncio
async def test_a_successful_placement_records_the_receipt(tmp_path) -> None:
    calls = Calls(answers={"sportsbet_place_bet": {"betId": "B99"}})
    ledger = Ledger(tmp_path / "l.jsonl")
    out = await run_intent(intent(), policy=auto_policy(), ledger=ledger, call=calls, now=NOON)
    assert out.status == "placed"
    assert out.receipt["betId"] == "B99"
    assert "confirm via sportsbet_bet_history" in out.reason


# ─── 6: everything lands in the ledger ─────────────────────────────────


@pytest.mark.asyncio
async def test_every_outcome_is_recorded_including_refusals(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    await run_intent(intent(edge=0.001), policy=auto_policy(), ledger=ledger, call=Calls(), now=NOON)
    await run_intent(intent(), policy=auto_policy(), ledger=ledger,
                     call=Calls(answers={"sportsbet_place_bet": {"betId": "B1"}}), now=NOON)
    statuses = [e.status for e in ledger]
    assert statuses == ["skipped", "placed"]


@pytest.mark.asyncio
async def test_only_placed_bets_spend_budget(tmp_path) -> None:
    """A week of paper running must not lock out the first real bet."""
    ledger = Ledger(tmp_path / "l.jsonl")
    for _ in range(5):
        await run_intent(intent(), policy=BettingPolicy(base_stake=10.0), ledger=ledger,
                         call=Calls(), now=NOON)
    assert ledger.staked_on(NOON.date()) == 0.0
    assert ledger.bets_on(NOON.date()) == 0


@pytest.mark.asyncio
async def test_the_ledger_feeds_the_next_budget_decision(tmp_path) -> None:
    """Spend is derived from the ledger, so two runs in a row see each other."""
    ledger = Ledger(tmp_path / "l.jsonl")
    policy = auto_policy(base_stake=10.0, daily_cap=15.0)
    calls = Calls(answers={"sportsbet_place_bet": {"betId": "B1"}})
    first = await run_intent(intent(), policy=policy, ledger=ledger, call=calls, now=NOON)
    second = await run_intent(intent(), policy=policy, ledger=ledger, call=calls, now=NOON)
    assert first.status == "placed" and first.stake == 10.0
    assert second.status == "placed" and second.stake == 5.0   # trimmed to the cap


# ─── the drift gate in isolation ───────────────────────────────────────


def test_drift_is_one_sided() -> None:
    assert drift.check(quoted=3.0, current=3.6, tolerance=0.02).ok       # drifted out
    assert not drift.check(quoted=3.0, current=2.5, tolerance=0.02).ok   # shortened


def test_drift_returns_the_fresh_price_to_place_at() -> None:
    r = drift.check(quoted=3.0, current=3.02, tolerance=0.05)
    assert r.ok and r.price == 3.02


def test_drift_rejects_a_non_price() -> None:
    assert not drift.check(quoted=3.0, current=0.0, tolerance=0.02).ok
    assert not drift.check(quoted=1.0, current=3.0, tolerance=0.02).ok


async def test_the_ledger_is_stamped_with_the_clock_the_budget_used(tmp_path) -> None:
    """The mechanism behind the cap holding, pinned directly.

    `today` is derived from `now`, while `staked_on(today)` reads the day off each
    entry's `at`. When `at` came from a second, fresh clock reading the two could
    disagree, and a bet budgeted against one day was recorded on another — after which
    the next bet saw a spend of zero and the daily cap did not bind.

    Asserting the recorded timestamp equals the decision's clock is what stops that
    returning by another route; asserting only the trimmed stake would not catch a
    re-introduction that happened to keep the two readings on the same side of midnight.
    """
    ledger = Ledger(tmp_path / "l.jsonl")
    await run_intent(intent(), policy=auto_policy(base_stake=10.0, daily_cap=50.0),
                     ledger=ledger, call=Calls(answers={"sportsbet_place_bet": {"betId": "B1"}}),
                     now=NOON)
    rows = list(ledger)
    assert len(rows) == 1
    assert datetime.fromisoformat(rows[0].at) == NOON, (
        "the entry was stamped from a different clock than the budget decision used")
