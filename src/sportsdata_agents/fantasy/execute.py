"""The single path from an intent to a change on someone's real team.

Every write goes through `run_intent`. Not because it is convenient, but because it is
the only way to guarantee that four things happen in the same order every time:

    1. the policy is consulted BEFORE a request is built
    2. anything not covered by policy becomes a proposal and stops
    3. the write happens once — never retried, because a retried transfer is a second
       transfer, and FPL will happily charge you for both
    4. the result is read back and compared to what was intended

Step 4 is the one that is easy to skip and expensive to have skipped. FPL's write
endpoints are undocumented; they can return 200 and do something other than what you
asked. The owner finds out on Saturday. So a write is not "done" here until the squad has
been re-read and matched against the intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .approvals import Proposal, State, Store, new_proposal, notify
from .policy import Decision, Verdict
from .verify import VerifyResult, escalate, verify_lineup, verify_transfers


class ToolCaller(Protocol):
    """Whatever can call an MCP tool. Kept abstract so this module can be tested without
    a live FPL session — the tests below are the reason the write path is trustworthy."""

    async def __call__(self, tool: str, **kwargs: Any) -> Any: ...


@dataclass
class Intent:
    """What the agent wants to do, before anyone has agreed to it."""

    action: str                 # "lineup" | "transfer" | "chip"
    entry: int
    summary: str
    diff: list[str]
    payload: dict
    deadline: datetime
    cost_points: int = 0
    platform: str = "fpl"


@dataclass
class Outcome:
    status: str                 # "acted" | "proposed" | "skipped" | "failed"
    detail: str
    proposal: Proposal | None = None
    verification: VerifyResult | None = None

    @property
    def changed_anything(self) -> bool:
        return self.status == "acted"


async def run_intent(
    intent: Intent, decision: Decision, *, call: ToolCaller, store: Store | None = None,
    csrf: str = "", approved: Proposal | None = None,
) -> Outcome:
    """Apply a policy decision to an intent.

    `approved` is passed when the owner has already said yes to a proposal — that is the
    only way an ASK becomes a write, and the proposal is re-checked for expiry inside
    `Store.approve` before it gets here.
    """
    store = store or Store.load()

    if decision.verdict is Verdict.SKIP:
        return Outcome("skipped", decision.reason)

    if decision.verdict is Verdict.ASK and approved is None:
        p = store.add(new_proposal(
            platform=intent.platform, entry=intent.entry, action=intent.action,
            summary=intent.summary, diff=intent.diff, payload=intent.payload,
            expires_at=intent.deadline, reason=decision.reason,
            cost_points=intent.cost_points,
        ))
        await notify(p)
        return Outcome("proposed", f"awaiting approval ({decision.reason})", proposal=p)

    if approved is not None and approved.state is not State.APPROVED:
        # Defence in depth: the store already refuses, but a caller that reached here
        # with an unapproved proposal has a bug, and the bug must not reach the API.
        return Outcome("skipped", f"proposal is {approved.state.value}, not approved")

    return await _write_and_verify(intent, call=call, csrf=csrf, store=store, proposal=approved)


async def _write_and_verify(
    intent: Intent, *, call: ToolCaller, csrf: str, store: Store, proposal: Proposal | None,
) -> Outcome:
    before = await _read_squad(call, intent.entry)

    try:
        if intent.action == "lineup":
            await call("fpl_set_lineup", managerId=intent.entry, csrf=csrf, **intent.payload)
        elif intent.action == "transfer":
            await call("fpl_transfers", csrf=csrf, **intent.payload)
        else:
            return Outcome("skipped", f"no write path for action {intent.action!r}")
    except Exception as e:
        # NOT retried. A transfer that timed out may still have been applied; sending it
        # again is how you pay a second points hit for one move.
        detail = f"{type(e).__name__}: {e}"
        if proposal:
            store.record_outcome(proposal.id, ok=False, detail=detail)
        return Outcome("failed", f"the write raised and was NOT retried — {detail}")

    after = await _read_squad(call, intent.entry)
    if intent.action == "lineup":
        result = verify_lineup(intent.payload.get("picks", []), after)
    else:
        result = verify_transfers(intent.payload.get("transfers", []), before, after)

    await escalate(result)
    if proposal:
        store.record_outcome(proposal.id, ok=result.ok, detail=result.summary)
    return Outcome(
        "acted" if result.ok else "failed",
        result.summary,
        proposal=proposal,
        verification=result,
    )


async def _read_squad(call: ToolCaller, entry: int) -> list[dict]:
    """The squad as FPL currently reports it.

    Returns [] rather than raising when the read fails, which makes the verifier report
    a mismatch — the safe direction. Claiming success because the read-back broke would
    be the unsafe one, and it is exactly the shortcut that makes silent failures silent.
    """
    try:
        body = await call("fpl_my_team", managerId=entry)
    except Exception:
        return []
    if isinstance(body, dict):
        return list(body.get("picks") or [])
    return []
