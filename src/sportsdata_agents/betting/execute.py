"""The single path from a candidate bet to money leaving an account.

Everything goes through `run_intent`, so that six things happen in the same order every
time and none of them can be skipped by a caller in a hurry:

    1. the policy decides BEFORE any request is built
    2. anything not cleared for unattended placement is recorded and stops
    3. the price is fetched again and the drift gate re-checks the edge
    4. the bet is placed ONCE — never retried
    5. the book's answer is read as a verdict, not as an HTTP status
    6. the outcome is written to the ledger whatever it was

Step 4 is the one that looks like a bug until it costs money. None of Sportsbet, Entain
or Unibet gives a usable idempotency key: their receipts are returned, not sent, so a
resent request is a SECOND BET rather than a retry of the first. Only TAB issues a key
that makes a resend safe. A placement that times out is therefore left alone and
escalated, because "did that land?" is a question to answer by reading the account, not
by asking again with money.

Step 5 exists because a 200 is not a placement. Entain returns 200 with `status` in the
body; Kambi (Unibet) does the same; Sportsbet answers 202 meaning "taken for
processing", which is not the same as "on". `_verdict_of` reads the body.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import drift
from .ledger import Entry, Ledger, now_iso
from .policy import BettingPolicy, Decision, Verdict


class ToolCaller(Protocol):
    """Whatever can call an MCP tool. Abstract so the whole money path is testable
    without a live bookmaker session — which is the only reason it can be trusted."""

    async def __call__(self, tool: str, /, **kwargs: Any) -> Any: ...


#: How each book's placement tool is named, and how to re-price before sending.
#: Kept as data so adding a book is a table entry rather than a branch in the executor.
PLACE_TOOL = {
    "sportsbet": "sportsbet_place_bet",
    "tab": "tab_place_bet",
    "entain": "entain_place_bet",
    "unibet": "unibet_place_bet",
}

#: NOT validate_coupon for Unibet. Kambi's validate answers {status, validSession,
#: rewardInfo} and echoes NO price, so a drift check reading it finds nothing and the
#: executor refuses every placement. adapters.reprice_args_for and live.read_unibet_price
#: were both retargeted at the anonymous pricer; this table was missed, which put the
#: bug straight back under a commit message claiming it fixed. Adding a book means
#: touching THREE places — this table, the args builder, and the reader — and a test now
#: asserts all three agree.
#: The book's own pre-placement check, where it has one. Runs AFTER the drift gate and
#: BEFORE the placement, because it answers a different question: drift asks "is the
#: price still there", validation asks "would this coupon be accepted at all, and is my
#: credential alive". Kambi's is anonymous and free, so a dead token costs nothing to
#: discover here instead of on the money call.
VALIDATE_TOOL = {
    "unibet": "unibet_validate_coupon",
}

REPRICE_TOOL = {
    "sportsbet": "sportsbet_price_slip",
    "tab": "tab_price_slip",
    "entain": "entain_sgm_price",
    "unibet": "unibet_sgm_price",
}


@dataclass
class Intent:
    """A bet the scanner wants, before anyone or anything has agreed to it."""

    book: str
    legs: list[dict]
    odds: float                    # the price the edge was computed on
    edge: float
    #: The provider-shaped body the placement tool wants. Built by the scanner's
    #: book adapter; the executor does not construct bookmaker payloads itself.
    payload: dict
    #: How to re-price this exact bet, as kwargs for the book's reprice tool.
    reprice_args: dict = field(default_factory=dict)
    #: How to ask the book whether it would accept this coupon at all, where it offers
    #: such a call. Empty for books that do not.
    validate_args: dict = field(default_factory=dict)
    summary: str = ""
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class Outcome:
    intent_id: str
    status: str
    reason: str
    stake: float = 0.0
    price: float = 0.0
    receipt: dict = field(default_factory=dict)


async def run_intent(
    intent: Intent,
    *,
    policy: BettingPolicy,
    ledger: Ledger,
    call: ToolCaller,
    now: datetime | None = None,
    on_ask: Callable[[Intent, Decision], Awaitable[None]] | None = None,
    reprice: Callable[[Any], float] | None = None,
) -> Outcome:
    """Run one candidate bet all the way through. Never raises for an ordinary refusal —
    a refusal is an outcome and is recorded as one."""
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()

    decision = policy.decide(
        book=intent.book,
        edge=intent.edge,
        odds=intent.odds,
        now=now,
        staked_today=ledger.staked_on(today),
        bets_today=ledger.bets_on(today),
        open_exposure=ledger.open_exposure(),
    )

    def record(status: str, reason: str, *, stake: float = 0.0, price: float = 0.0,
               receipt: dict | None = None) -> Outcome:
        ledger.append(Entry(
            intent_id=intent.intent_id, at=now_iso(), status=status,  # type: ignore[arg-type]
            book=intent.book, reason=reason, legs=intent.legs,
            stake=stake, odds=price or intent.odds, edge=intent.edge,
            receipt=receipt or {},
        ))
        return Outcome(intent.intent_id, status, reason, stake, price or intent.odds, receipt or {})

    if decision.verdict is Verdict.SKIP:
        return record("skipped", decision.reason)

    stake = decision.stake or 0.0

    if decision.verdict is Verdict.PAPER:
        return record("paper", decision.reason, stake=stake)

    if decision.verdict is Verdict.ASK:
        out = record("asked", decision.reason, stake=stake)
        if on_ask is not None:
            await on_ask(intent, decision)
        return out

    # ─── Verdict.PLACE — the only branch that can move money ────────────

    # 3. Re-price. A book that cannot quote right now is a book we do not place at.
    price = intent.odds
    tool = REPRICE_TOOL.get(intent.book)
    if tool and intent.reprice_args:
        try:
            quote = await call(tool, **intent.reprice_args)
        except Exception as exc:
            return record("rejected", f"re-price failed, not placing: {exc}", stake=stake)
        if reprice is None:
            return record("rejected", "no re-price reader supplied for this book; refusing to place blind", stake=stake)
        try:
            price = reprice(quote)
        except Exception as exc:
            return record("rejected", f"could not read the re-quoted price, not placing: {exc}", stake=stake)

        gate = drift.check(quoted=intent.odds, current=price, tolerance=policy.max_price_drift)
        if not gate.ok:
            return record("rejected", gate.reason, stake=stake, price=price)
        price = gate.price  # place at the book's number, never the remembered one

    # 3b. The book's own go/no-go, where it offers one. A refusal here is a bet that
    # would not have been accepted, so it never reaches the money call.
    check_tool = VALIDATE_TOOL.get(intent.book)
    if check_tool and intent.validate_args:
        try:
            verdict = await call(check_tool, **intent.validate_args)
        except Exception as exc:
            return record("rejected", f"pre-placement check failed, not placing: {exc}",
                          stake=stake, price=price)
        ok, why = _validation_of(intent.book, verdict)
        if not ok:
            return record("rejected", f"book would not accept this: {why}",
                          stake=stake, price=price)

    # 4. Place. ONCE. No retry wrapper here, deliberately — see the module docstring.
    place_tool = PLACE_TOOL.get(intent.book)
    if not place_tool:
        return record("rejected", f"no placement tool for {intent.book}", stake=stake, price=price)

    payload = dict(intent.payload)
    try:
        answer = await call(place_tool, **payload)
    except Exception as exc:
        # An exception here may mean the bet did NOT go on, or that it did and the
        # answer was lost. Both look identical from here, so it is escalated rather
        # than resolved — and never resent.
        return record(
            "rejected",
            f"placement failed or timed out: {exc}. NOT retried — verify by reading the "
            f"account before assuming this bet is not on.",
            stake=stake, price=price,
        )

    # 5. Read the body, not the status code.
    ok, reason, receipt = _verdict_of(intent.book, answer)
    if not ok:
        return record("rejected", f"book refused: {reason}", stake=stake, price=price, receipt=receipt)
    return record("placed", reason, stake=stake, price=price, receipt=receipt)


def _validation_of(book: str, answer: Any) -> tuple[bool, str]:
    """Did the book's pre-placement check pass?

    Kambi answers {status: "SUCCESS", validSession: bool, rewardInfo: {...}} — verified
    live 2026-08-27. `validSession` is the cheapest way to tell a dead bearer token from
    a bad coupon, and distinguishing the two matters: one needs a new credential, the
    other needs a different bet.
    """
    body = answer if isinstance(answer, dict) else {"raw": answer}
    if book == "unibet":
        if body.get("validSession") is False:
            return False, "kambi reports the session is dead — the access token needs refreshing"
        status = str(body.get("status", "")).upper()
        if status and status != "SUCCESS":
            return False, f"kambi status={status}: {body.get('message', '')}".strip()
        return True, "kambi accepted the coupon"
    return True, "no pre-placement check for this book"


def _verdict_of(book: str, answer: Any) -> tuple[bool, str, dict]:
    """Did the bet actually go on? Every book answers differently and none of them
    answer with the HTTP status alone."""
    body = answer if isinstance(answer, dict) else {"raw": answer}

    if book == "entain":
        status = str(body.get("status", "")).lower()
        return status == "accepted", f"entain status={status or 'missing'}", body

    if book == "unibet":
        # Kambi puts a rejection in {status, message}; a placed coupon does not carry a
        # rejection envelope. The success body has not been observed, so anything
        # carrying a `message` alongside a non-empty `status` is treated as a refusal
        # and anything else is reported as placed-but-unconfirmed.
        if "message" in body and body.get("status") not in (None, "", 0):
            return False, f"kambi says {body.get('status')}: {body.get('message')}", body
        return True, "kambi accepted the coupon (success body unpinned — confirm by reading the account)", body

    if book == "sportsbet":
        # 202 Accepted means taken for processing, not on. A betId is the receipt.
        bet_id = body.get("betId") or body.get("betid")
        if not bet_id:
            return False, "no betId returned", body
        return True, f"accepted for processing as {bet_id} — confirm via sportsbet_bet_history", body

    if book == "tab":
        status = str(body.get("status", "")).lower()
        return status in ("ok", "accepted"), f"tab status={status or 'missing'}", body

    return False, f"no verdict rule for {book}", body
