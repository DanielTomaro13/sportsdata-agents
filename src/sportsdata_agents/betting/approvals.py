"""The proposal a human approves before money moves, and the expiry that keeps it honest.

Mirrors `fantasy.approvals` deliberately — same Store-on-disk shape, same notification
router — because an owner should not have to learn two approval systems. One thing is
genuinely different, and it is the important one.

## A bet proposal goes stale in minutes, not at a deadline

A fantasy proposal is valid until the gameweek deadline: hours, sometimes days. A bet
proposal is valid until the price moves, which can be seconds. So the default TTL here is
minutes, and two rules follow from it:

1. **An expired proposal is never executed.** Not "executed with a warning" — a price
   that has expired is not a price, and the bet that was approved is not the bet that
   would be placed.

2. **Approval does NOT skip the drift gate.** This is the rule most likely to be
   "optimised away" by someone reasoning that a human already said yes. They said yes to
   a number. `execute.run_intent` re-prices and re-checks drift on an approved intent
   exactly as it does on an automatic one, and the approval carries the price that was
   agreed so the gate has something to compare against.

Both exist because the failure they prevent is silent: the bet still gets placed, it just
gets placed at a worse price than the one anyone agreed to.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .ledger import atomic_write

#: How long a proposed bet stays approvable. Short on purpose — see the module docstring.
DEFAULT_TTL_MINUTES = 10.0


class State(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    PLACED = "placed"
    FAILED = "failed"


@dataclass
class BetProposal:
    """One bet awaiting a human, and everything needed to judge it."""

    id: str
    book: str
    fixture_id: str
    summary: str                    # one line, for the notification
    legs: list[dict]
    stake: float
    #: The price the edge was computed on, and the number the human is agreeing to. The
    #: drift gate compares against THIS on execution, not against a fresher memory.
    odds: float
    edge: float
    edge_basis: str
    created_at: str
    expires_at: str
    reason: str = ""                # why the policy asked rather than placed
    #: Exactly what would be sent, built by the book's adapter at proposal time so the
    #: human is approving a real request rather than a description of one.
    payload: dict = field(default_factory=dict)
    #: Book-specific extras the executor needs (TAB's transactionId, say).
    context: dict = field(default_factory=dict)
    state: State = State.PENDING
    outcome: str = ""

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        return now >= datetime.fromisoformat(self.expires_at)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BetProposal:
        d = dict(d)
        d["state"] = State(d["state"])
        return cls(**d)

    def as_notification(self) -> str:
        legs = "\n".join(f"  • {_leg_line(leg)}" for leg in self.legs)
        mins = max(0, (datetime.fromisoformat(self.expires_at)
                       - datetime.now(tz=UTC)).total_seconds() / 60)
        return (
            f"BET PROPOSAL {self.id[:8]} — {self.book}\n"
            f"{self.summary}\n"
            f"{legs}\n"
            f"${self.stake:.2f} at {self.odds:.2f} "
            f"({self.edge:+.2%} {self.edge_basis})\n"
            f"{self.reason}\n"
            f"Expires in {mins:.0f} min — approve: agents bet approve {self.id[:8]}"
        )


def _leg_line(leg: dict) -> str:
    """One leg, printed from typed fields only.

    Deliberately does NOT echo arbitrary free text from a bookmaker payload: this string
    goes into a notification a human acts on, and a leg description is attacker-adjacent
    content. Known keys, or a plain repr of the shape.
    """
    parts = [str(leg[k]) for k in ("market", "selection", "line") if leg.get(k) is not None]
    return " ".join(parts) if parts else f"<leg: {sorted(leg)}>"


class Store:
    """Proposals on disk. Small enough to rewrite whole, and rewritten atomically so a
    crash mid-save cannot leave a file that parses as an empty set of proposals."""

    def __init__(self, path: Path, proposals: list[BetProposal] | None = None) -> None:
        self.path = path
        self.proposals = proposals or []

    @classmethod
    def load(cls, path: Path) -> Store:
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            return cls(path)
        return cls(path, [BetProposal.from_dict(d) for d in raw.get("proposals", [])])

    def save(self) -> None:
        atomic_write(self.path, json.dumps(
            {"proposals": [p.to_dict() for p in self.proposals]}, indent=2))

    def add(self, p: BetProposal) -> BetProposal:
        self.proposals.append(p)
        self.save()
        return p

    def find(self, prefix: str) -> BetProposal | None:
        hits = [p for p in self.proposals if p.id.startswith(prefix)]
        return hits[0] if len(hits) == 1 else None

    def pending(self, now: datetime | None = None) -> list[BetProposal]:
        """Live proposals only. Expiry is applied on READ as well as on approve, so a
        stale proposal is never shown as actionable just because nothing ran since."""
        now = now or datetime.now(tz=UTC)
        out = []
        for p in self.proposals:
            if p.state is not State.PENDING:
                continue
            if p.is_expired(now):
                p.state = State.EXPIRED
                p.outcome = "expired before anyone approved it"
                continue
            out.append(p)
        return out

    def approve(self, prefix: str, now: datetime | None = None) -> tuple[BetProposal | None, str]:
        now = now or datetime.now(tz=UTC)
        p = self.find(prefix)
        if p is None:
            return None, f"no single proposal matches {prefix!r}"
        if p.state is not State.PENDING:
            return p, f"proposal is already {p.state.value}"
        if p.is_expired(now):
            p.state = State.EXPIRED
            p.outcome = "expired before approval"
            self.save()
            return p, (
                "that proposal has expired — the price it quoted is no longer the price. "
                "Re-scan rather than approving a stale number."
            )
        p.state = State.APPROVED
        self.save()
        return p, "approved"

    def reject(self, prefix: str) -> tuple[BetProposal | None, str]:
        p = self.find(prefix)
        if p is None:
            return None, f"no single proposal matches {prefix!r}"
        p.state = State.REJECTED
        self.save()
        return p, "rejected"

    def approved(self, now: datetime | None = None) -> list[BetProposal]:
        """Approved AND still live. An approval does not stop the clock — see the module
        docstring: a price approved twenty minutes ago is not a price."""
        now = now or datetime.now(tz=UTC)
        out = []
        for p in self.proposals:
            if p.state is not State.APPROVED:
                continue
            if p.is_expired(now):
                p.state = State.EXPIRED
                p.outcome = "approved, but expired before it could be placed"
                continue
            out.append(p)
        return out

    def record_outcome(self, pid: str, *, ok: bool, detail: str) -> None:
        for p in self.proposals:
            if p.id == pid:
                p.state = State.PLACED if ok else State.FAILED
                p.outcome = detail
                self.save()
                return


def new_proposal(
    *,
    book: str,
    fixture_id: str,
    summary: str,
    legs: list[dict],
    stake: float,
    odds: float,
    edge: float,
    edge_basis: str,
    reason: str = "",
    payload: dict | None = None,
    context: dict | None = None,
    ttl_minutes: float = DEFAULT_TTL_MINUTES,
    now: datetime | None = None,
) -> BetProposal:
    now = now or datetime.now(tz=UTC)
    return BetProposal(
        id=uuid.uuid4().hex,
        book=book,
        fixture_id=fixture_id,
        summary=summary,
        legs=legs,
        stake=stake,
        odds=odds,
        edge=edge,
        edge_basis=edge_basis,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(),
        reason=reason,
        payload=payload or {},
        context=context or {},
    )


def alert_channel() -> str:
    """Where bet proposals go. Defaults to 'log' so an unconfigured install is quiet
    rather than erroring — the proposal is on disk either way."""
    return os.environ.get("BETTING_ALERT_CHANNEL", "log")


async def notify(proposal: BetProposal, channel: str | None = None) -> bool:
    """Best-effort: a notification that fails must not lose the proposal, which is
    already saved."""
    from ..observability.notify import push_to_channel

    return await push_to_channel(channel or alert_channel(), proposal.as_notification())
