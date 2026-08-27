"""One pass of the whole plane: compare, score, decide, and act on what clears.

This is the only module that knows the pieces fit together in a particular order, which
keeps every other module ignorant of the ones around it — the scanner has no opinion
about placing, the policy has never heard of a bookmaker payload, and the executor takes
an abstract caller.

Two entry points:

    scan_fixture   — comparison → scored candidates → a decision per book
    place_approved — the other half of `ask` mode: a human said yes, now go

## The best price is not automatically the bet

`scan_fixture` scores every book and then acts on the BEST candidate only. Placing the
same combination at several books is not diversification — it is the same opinion, at
several prices, consuming the budget several times. If the field is right and the outlier
is wrong, every one of those bets is wrong together.

## An approval is not a bypass

`place_approved` runs the identical `run_intent` path an automatic placement runs, with
the same re-price and the same drift gate. The human agreed to a number, and this checks
the number is still there. See `approvals`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import adapters, approvals
from .execute import Intent, Outcome, ToolCaller, run_intent
from .ledger import Ledger
from .policy import BettingPolicy
from .scanner import Candidate, candidates_from_comparison

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    fixture_id: str
    candidates: list[Candidate] = field(default_factory=list)
    outcome: Outcome | None = None
    proposal_id: str | None = None
    note: str = ""

    def summary(self) -> str:
        if not self.candidates:
            return f"{self.fixture_id}: {self.note or 'no candidates'}"
        best = self.candidates[0]
        tail = f" → {self.outcome.status}: {self.outcome.reason}" if self.outcome else ""
        return f"{self.fixture_id}: {best.summary()}{tail}"


async def scan_fixture(
    *,
    comparison: dict[str, Any],
    fixture_id: str,
    policy: BettingPolicy,
    ledger: Ledger,
    call: ToolCaller,
    store: approvals.Store | None = None,
    now: datetime | None = None,
    reprice: Callable[[Any], float] | None = None,
    reprice_args: dict | None = None,
    notify: bool = True,
) -> ScanResult:
    """Score one comparison and act on the best candidate the policy will take."""
    now = now or datetime.now(UTC)

    candidates = candidates_from_comparison(
        comparison,
        fixture_id=fixture_id,
        basis=policy.edge_basis,
        assumed_overround=policy.assumed_overround,
        books=set(policy.books) if policy.books else None,
    )
    if not candidates:
        return ScanResult(fixture_id, note=(
            comparison.get("note")
            or "not enough books priced this combination to form a consensus"
        ))

    best = candidates[0]

    # Build the payload BEFORE asking the policy to place, so a book we cannot construct
    # a request for is discovered here rather than halfway through the money path — and
    # so an `ask` proposal carries the real request a human is agreeing to.
    stake = policy.size(edge=best.edge, odds=best.odds)
    try:
        payload = adapters.payload_for(best, stake=stake)
    except adapters.AdapterError as exc:
        return ScanResult(fixture_id, candidates, note=f"cannot build a placement for {best.book}: {exc}")

    intent = Intent(
        book=best.book,
        legs=best.legs,
        odds=best.odds,
        edge=best.edge,
        payload=payload,
        reprice_args=reprice_args or {},
        summary=best.summary(),
    )

    async def on_ask(i: Intent, decision) -> None:
        if store is None:
            log.warning("policy asked for approval but no proposal store was supplied")
            return
        proposal = approvals.new_proposal(
            book=i.book, fixture_id=fixture_id, summary=i.summary, legs=i.legs,
            stake=decision.stake or stake, odds=i.odds, edge=i.edge,
            edge_basis=best.edge_basis, reason=decision.reason, payload=i.payload,
            # Carried so the approved bet can be RE-PRICED when it is finally placed.
            # Without this the drift gate has nothing to fetch and an approval would
            # silently become the bypass this plane exists to avoid.
            context={"reprice_args": i.reprice_args},
        )
        store.add(proposal)
        result.proposal_id = proposal.id
        if notify:
            await approvals.notify(proposal)

    result = ScanResult(fixture_id, candidates)
    result.outcome = await run_intent(
        intent, policy=policy, ledger=ledger, call=call,
        now=now, on_ask=on_ask, reprice=reprice,
    )
    return result


async def place_approved(
    *,
    store: approvals.Store,
    policy: BettingPolicy,
    ledger: Ledger,
    call: ToolCaller,
    now: datetime | None = None,
    reprice: Callable[[Any], float] | None = None,
) -> list[Outcome]:
    """Place every approved, unexpired proposal — each through the full money path.

    The policy is consulted AGAIN. A human approving a bet does not suspend the daily
    cap or the exposure ceiling: those exist to bound the total, and an approval is about
    this bet rather than about the budget.
    """
    out: list[Outcome] = []
    for proposal in store.approved(now):
        # Force the placing branch for this one bet: the human already answered the
        # ask. Everything else — budget, drift, the single attempt — still applies.
        placing = _as_placing_policy(policy, proposal.book)
        intent = Intent(
            book=proposal.book,
            legs=proposal.legs,
            odds=proposal.odds,
            edge=proposal.edge,
            payload=proposal.payload,
            # The whole reason `context` exists: an approved bet is re-priced and
            # drift-checked exactly like an automatic one. See the module docstring.
            reprice_args=proposal.context.get("reprice_args") or {},
            summary=proposal.summary,
            intent_id=proposal.id[:12],
        )
        outcome = await run_intent(
            intent, policy=placing, ledger=ledger, call=call, now=now, reprice=reprice,
        )
        store.record_outcome(proposal.id, ok=outcome.status == "placed", detail=outcome.reason)
        out.append(outcome)
    return out


def _as_placing_policy(policy: BettingPolicy, book: str) -> BettingPolicy:
    """A copy of the policy with THIS book set to place, because the human said so.

    A copy rather than a mutation: the caller's policy is often long-lived, and a
    one-bet approval must not quietly turn a book to `auto` for everything after it.
    `allow_unverified_auto` is set for the same reason the human was asked in the first
    place — they looked at it and agreed.
    """
    import copy

    p = copy.deepcopy(policy)
    p.book_modes = {**p.book_modes, book: "auto"}
    p.books = sorted({*p.books, book}) if p.books else p.books
    p.allow_unverified_auto = True
    p.quiet_hours = None  # a human is demonstrably awake
    return p
