"""What the agent may do to a fantasy team without being asked.

The write tools exist and work. The question this module answers is a different one:
given a decision the agent wants to make, does it *act*, *ask*, or *stay out of it*?

That is a per-owner, per-action judgement, and it should be a setting rather than a
prompt instruction — a system prompt is advice a model can talk itself out of, while a
policy is a gate the code enforces before a request is built.

    lineup:    auto           # set the XI before each deadline
    captain:   auto
    transfers: auto_if_free   # use a free transfer; never take a points hit
    chips:     ask            # wildcard/free-hit/bench-boost/triple-captain
    max_hit:   0              # points willing to spend without asking

THE DEFAULTS ARE DELIBERATELY TIMID. Everything starts at `ask`. An owner who wants an
autonomous agent opts into it action by action, because the failure mode of an agent
that acts too freely (a wildcard played in October) is a whole season, while the failure
mode of one that asks too often is a notification.

CHIPS CAN NEVER BE `auto`. Not a default — a rule the model rejects. There are four of
them in a season, each worth many points if played well and unrecoverable if wasted, and
"the agent played my wildcard on a blank gameweek" is not a mistake anyone should be able
to configure their way into by accident.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

Mode = Literal["auto", "auto_if_free", "ask", "never"]


class Verdict(StrEnum):
    """What the policy decided about one proposed action."""

    ACT = "act"      # inside policy — execute, then verify and report
    ASK = "ask"      # route to the owner and wait
    SKIP = "skip"    # the owner does not want this done at all


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    #: True when the action itself is fine but the timing is not (quiet hours).
    deferred: bool = False


@dataclass
class LeaguePolicy:
    """One team's settings. Serialised per (platform, entry)."""

    #: Identity a platform needs beyond `entry`, validated at construction so a policy
    #: that could never execute cannot be saved in the first place.
    REQUIRED_CONTEXT: ClassVar[dict[str, tuple[str, ...]]] = {
        "espn": ("leagueId", "seasonId", "game"),
        "mfl": ("leagueId", "year"),
    }

    #: Platforms with ONE hard lock for the whole team. FPL has a gameweek deadline: a
    #: real instant, and "act within N hours of it" means waiting for team news.
    #:
    #: ESPN has no such moment — a fantasy week rolls over and each player locks at his
    #: own kickoff. Its horizon is therefore a rolling `now + N`, which never counts
    #: down, so the too-early rule would be a constant: permanently open or permanently
    #: shut, never a window. On those platforms the rule is skipped and the real bound is
    #: the once-per-period run trigger.
    HARD_DEADLINE: ClassVar[frozenset[str]] = frozenset({"fpl"})

    platform: str
    entry: int

    lineup: Mode = "ask"
    captain: Mode = "ask"
    transfers: Mode = "ask"
    chips: Mode = "ask"

    #: Points the owner will spend on transfers without being asked. 0 means free ones
    #: only — the setting most owners actually want, and the default for that reason.
    max_hit: int = 0
    #: Never act inside this window; hold the proposal until it closes.
    quiet_hours: tuple[str, str] | None = ("23:00", "07:00")
    #: A ceiling on autonomous actions per gameweek. A policy bug that proposes forever
    #: should exhaust a budget, not an owner's patience.
    max_actions_per_gameweek: int = 3
    #: Act only when the deadline is inside this many hours — late enough that team news
    #: has landed, early enough to leave room to fix a failure.
    act_within_hours_of_deadline: float = 6.0

    #: Whatever the platform needs to identify the team beyond `entry`. FPL needs
    #: nothing. ESPN needs {leagueId, seasonId, game} — all public identifiers straight
    #: out of the league URL, never credentials. Stored with the policy so the scheduler
    #: can act on a team without a human present to supply them.
    context: dict = field(default_factory=dict)

    notes: list[str] = field(default_factory=list)

    # ─── the rule that is not configurable ──────────────────────────────

    def __post_init__(self) -> None:
        if self.chips in ("auto", "auto_if_free"):
            raise ValueError(
                "chips cannot be automatic. There are four in a season, each is "
                "unrecoverable once played, and a badly-timed wildcard costs the season. "
                "Use 'ask' or 'never'."
            )
        if self.max_hit < 0:
            raise ValueError("max_hit cannot be negative")
        for key in self.REQUIRED_CONTEXT.get(self.platform, ()):
            if not self.context.get(key):
                raise ValueError(
                    f"{self.platform} needs {key!r} in context to identify the team — "
                    f"needs {sorted(self.REQUIRED_CONTEXT[self.platform])}, all readable "
                    "from the league URL"
                )

    # ─── decisions ──────────────────────────────────────────────────────

    def for_lineup(self, *, now: datetime, deadline: datetime, actions_taken: int = 0) -> Decision:
        return self._decide("lineup", self.lineup, now=now, deadline=deadline, actions_taken=actions_taken)

    def for_captain(self, *, now: datetime, deadline: datetime, actions_taken: int = 0) -> Decision:
        return self._decide("captain", self.captain, now=now, deadline=deadline, actions_taken=actions_taken)

    def for_chip(self, chip: str) -> Decision:
        """Always ASK or SKIP — see __post_init__."""
        if self.chips == "never":
            return Decision(Verdict.SKIP, f"policy: chips are off ({chip} not proposed)")
        return Decision(Verdict.ASK, f"{chip} is a chip — chips are never automatic")

    def for_transfer(
        self, *, hit: int, free_transfers: int, transfers_used: int,
        now: datetime, deadline: datetime, actions_taken: int = 0,
    ) -> Decision:
        """`hit` is the points cost (0 when covered by free transfers)."""
        if self.transfers == "never":
            return Decision(Verdict.SKIP, "policy: transfers are off")
        if self.transfers == "ask":
            return Decision(Verdict.ASK, "policy: transfers are set to ask")

        if self.transfers == "auto_if_free" and hit > 0:
            return Decision(
                Verdict.ASK,
                f"costs {hit} points and policy is auto_if_free "
                f"({free_transfers} free, {transfers_used} used)",
            )
        if hit > self.max_hit:
            return Decision(Verdict.ASK, f"costs {hit} points, above the {self.max_hit}-point limit")
        return self._decide("transfers", "auto", now=now, deadline=deadline, actions_taken=actions_taken)

    # ─── shared gating ──────────────────────────────────────────────────

    def _decide(
        self, what: str, mode: Mode, *, now: datetime, deadline: datetime, actions_taken: int
    ) -> Decision:
        if mode == "never":
            return Decision(Verdict.SKIP, f"policy: {what} is off")
        if mode in ("ask",):
            return Decision(Verdict.ASK, f"policy: {what} is set to ask")

        if deadline <= now:
            return Decision(Verdict.SKIP, "the deadline has passed — nothing can change now")

        if actions_taken >= self.max_actions_per_gameweek:
            return Decision(
                Verdict.ASK,
                f"already acted {actions_taken}x this gameweek (limit {self.max_actions_per_gameweek})",
            )

        hours_left = (deadline - now).total_seconds() / 3600
        if (self.platform in self.HARD_DEADLINE
                and hours_left > self.act_within_hours_of_deadline):
            return Decision(
                Verdict.SKIP,
                f"{hours_left:.1f}h until the deadline — too early; team news is still moving",
                deferred=True,
            )
        if self._in_quiet_hours(now):
            return Decision(Verdict.ASK, "inside quiet hours — not acting unattended", deferred=True)
        return Decision(Verdict.ACT, f"policy: {what} is automatic, {hours_left:.1f}h before the deadline")

    def _in_quiet_hours(self, now: datetime) -> bool:
        if not self.quiet_hours:
            return False
        start = time.fromisoformat(self.quiet_hours[0])
        end = time.fromisoformat(self.quiet_hours[1])
        t = now.timetz().replace(tzinfo=None)
        # A window that wraps midnight (23:00→07:00) is the normal case, so handle it
        # rather than treating it as invalid.
        return (start <= t or t < end) if start > end else (start <= t < end)

    # ─── persistence ────────────────────────────────────────────────────

    @property
    def key(self) -> str:
        """Unique per TEAM, not per team id.

        `entry` alone is unique on FPL (a manager id is global) but not on ESPN, where
        every league numbers its teams from 1 — so two leagues both have a team 4, and a
        bare `espn:4` key would let a policy for one silently govern the other.
        """
        parts = [self.platform, str(self.entry)]
        if league := self.context.get("leagueId"):
            parts.insert(1, str(league))
        return ":".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("quiet_hours"):
            d["quiet_hours"] = list(d["quiet_hours"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> LeaguePolicy:
        d = dict(d)
        if d.get("quiet_hours"):
            d["quiet_hours"] = tuple(d["quiet_hours"])
        return cls(**d)


def policies_path(base: Path | None = None) -> Path:
    from ..paths import data_dir

    return (base or data_dir()) / "fantasy-policies.json"


def load_policies(base: Path | None = None) -> dict[str, LeaguePolicy]:
    path = policies_path(base)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: LeaguePolicy.from_dict(v) for k, v in raw.items()}


def save_policy(policy: LeaguePolicy, base: Path | None = None) -> Path:
    path = policies_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text())
    existing[policy.key] = policy.to_dict()
    path.write_text(json.dumps(existing, indent=2))
    return path


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
