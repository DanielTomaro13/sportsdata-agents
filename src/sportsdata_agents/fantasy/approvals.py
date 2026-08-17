"""Proposals the owner has to approve, and the expiry that makes them safe.

When the policy says ASK, the agent writes a proposal here and notifies. The owner
approves or ignores it; nothing happens until they do.

THE EXPIRY IS THE POINT. A fantasy proposal is only valid until the deadline it was
computed for — "transfer Salah in" means nothing three hours after the gameweek locked,
and acting on a stale approval is worse than not acting at all. Every proposal therefore
carries an `expires_at`, and an expired one can never be approved, only re-proposed
against fresh data.

A proposal also stores the DIFF it intends to make, in human terms. That is what gets
notified, what the owner approves, and what the verifier checks afterwards — one
description, three uses, so there is no gap between what was agreed and what was done.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class State(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class Proposal:
    """One thing the agent wants to do, and everything needed to judge it."""

    id: str
    platform: str
    entry: int
    action: str                    # "lineup" | "transfer" | "chip"
    summary: str                   # one line, for the notification
    diff: list[str]                # the human-readable change, line by line
    payload: dict                  # exactly what would be sent
    created_at: str
    expires_at: str                # normally the gameweek deadline
    reason: str = ""               # why the policy asked rather than acted
    cost_points: int = 0
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
    def from_dict(cls, d: dict) -> Proposal:
        d = dict(d)
        d["state"] = State(d["state"])
        return cls(**d)

    def as_notification(self) -> str:
        """What the owner actually sees. Short enough for a phone lock screen, complete
        enough to decide without opening anything."""
        lines = [f"*{self.summary}*"]
        lines += [f"  {d}" for d in self.diff]
        if self.cost_points:
            lines.append(f"  cost: {self.cost_points} points")
        if self.reason:
            lines.append(f"  ({self.reason})")
        left = datetime.fromisoformat(self.expires_at) - datetime.now(tz=UTC)
        hours = max(0, left.total_seconds() / 3600)
        lines.append(f"  expires in {hours:.1f}h — approve with:  agents fantasy approve {self.id[:8]}")
        return "\n".join(lines)


@dataclass
class Store:
    """Proposals on disk. Small, append-mostly, and readable by a human in a text editor
    — this is the audit trail for actions taken on someone's team, so it should not need
    a tool to inspect."""

    path: Path
    proposals: dict[str, Proposal] = field(default_factory=dict)

    @classmethod
    def load(cls, base: Path | None = None) -> Store:
        from ..paths import data_dir

        path = (base or data_dir()) / "fantasy-proposals.json"
        store = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text())
            store.proposals = {k: Proposal.from_dict(v) for k, v in raw.items()}
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: p.to_dict() for k, p in self.proposals.items()}, indent=2)
        )

    def add(self, p: Proposal) -> Proposal:
        self.proposals[p.id] = p
        self.save()
        return p

    def find(self, prefix: str) -> Proposal | None:
        """Match on an id prefix — an owner approving from a phone should not have to
        type a uuid."""
        hits = [p for pid, p in self.proposals.items() if pid.startswith(prefix)]
        return hits[0] if len(hits) == 1 else None

    def pending(self, now: datetime | None = None) -> list[Proposal]:
        """Pending AND still valid. Expiry is applied on read, so a proposal that lapsed
        while nobody was looking cannot be approved later."""
        now = now or datetime.now(tz=UTC)
        out = []
        changed = False
        for p in self.proposals.values():
            if p.state is not State.PENDING:
                continue
            if p.is_expired(now):
                p.state = State.EXPIRED
                p.outcome = "expired before it was approved"
                changed = True
                continue
            out.append(p)
        if changed:
            self.save()
        return out

    def approve(self, prefix: str, now: datetime | None = None) -> tuple[Proposal | None, str]:
        p = self.find(prefix)
        if p is None:
            return None, "no proposal with that id (or the prefix matches several)"
        if p.state is not State.PENDING:
            return p, f"already {p.state.value}"
        if p.is_expired(now):
            p.state = State.EXPIRED
            p.outcome = "expired before it was approved"
            self.save()
            # The important refusal: acting on a stale approval is worse than not acting.
            return p, "expired — the deadline it was computed for has passed; re-run to get a fresh proposal"
        p.state = State.APPROVED
        self.save()
        return p, "approved"

    def reject(self, prefix: str) -> tuple[Proposal | None, str]:
        p = self.find(prefix)
        if p is None:
            return None, "no proposal with that id"
        p.state = State.REJECTED
        self.save()
        return p, "rejected"

    def record_outcome(self, pid: str, *, ok: bool, detail: str) -> None:
        p = self.proposals.get(pid)
        if p is None:
            return
        p.state = State.EXECUTED if ok else State.FAILED
        p.outcome = detail
        self.save()


def new_proposal(
    *, platform: str, entry: int, action: str, summary: str, diff: list[str],
    payload: dict, expires_at: datetime, reason: str = "", cost_points: int = 0,
) -> Proposal:
    return Proposal(
        id=uuid.uuid4().hex,
        platform=platform,
        entry=entry,
        action=action,
        summary=summary,
        diff=diff,
        payload=payload,
        created_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        expires_at=expires_at.isoformat(timespec="seconds"),
        reason=reason,
        cost_points=cost_points,
    )


def alert_channel() -> str:
    """Where fantasy proposals go. Defaults to 'log', so an unconfigured install is
    quiet rather than erroring — the proposal is on disk either way."""
    return os.environ.get("FANTASY_ALERT_CHANNEL", "log")


async def notify(proposal: Proposal, channel: str | None = None) -> bool:
    """Send a proposal through the platform's alert router. Best-effort: a notification
    that fails must not lose the proposal, which is already saved."""
    from ..observability.notify import push_to_channel

    return await push_to_channel(channel or alert_channel(), proposal.as_notification())
