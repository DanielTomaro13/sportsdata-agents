"""Read back after every write, because a 200 is not proof.

FPL's write endpoints are undocumented. They can accept a request, return 200, and not
do what you meant — a pick silently dropped for an illegal formation, a captain that did
not move, a transfer applied at a different price. The only way to know is to re-read the
squad and compare it to what was intended.

This module is that comparison. It takes the intended state and the state the provider
reports afterwards, and answers one question honestly: did the thing you agreed to
actually happen?

WHY THE FAILURE MATTERS MORE THAN THE SUCCESS. A write that fails loudly costs a
notification. A write that fails silently costs a gameweek — the owner believes the team
is set, and finds out on Saturday. So a mismatch here is an escalation, not a log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: FPL squads are 15, of whom 11 start. Used only to decide when a formation check is
#: meaningful — see verify_lineup.
SQUAD_SIZE = 15
XI_SIZE = 11

#: Platforms where a captain exists at all. ESPN fantasy has no armband — asserting one
#: there would report "NO CAPTAIN is set" on every correct write, which is how a verifier
#: teaches people to ignore it.
HAS_CAPTAIN = frozenset({"fpl"})

#: Platforms whose roster read does NOT report who is starting.
#:
#: MyFantasyLeague has no lineup EXPORT — `lineup` is an import-only type — so after
#: setting a lineup there is nothing to read back that says who starts. A full comparison
#: is therefore impossible, and both easy answers are wrong: asserting equality reports a
#: failure on every correct write, and skipping the check reports success without looking.
#:
#: So the check is narrowed to what the roster read CAN prove — that every intended
#: starter is actually on the roster, which still catches a mistyped id or a player who
#: is not yours — and the result SAYS the slots were not verified. A partial check
#: described as partial is worth more than a total one that is a fiction.
PARTIAL_LINEUP_READBACK = frozenset({"mfl"})


@dataclass
class VerifyResult:
    ok: bool
    #: Human-readable differences between intended and actual. Empty when ok.
    mismatches: list[str] = field(default_factory=list)
    summary: str = ""

    def as_notification(self) -> str:
        if self.ok:
            return f"✓ {self.summary}"
        lines = [f"⚠ WRITE DID NOT LAND AS INTENDED — {self.summary}"]
        lines += [f"  {m}" for m in self.mismatches]
        lines.append("  Your team may not be what you approved. Check it before the deadline.")
        return "\n".join(lines)


def _id(value: object) -> str:
    """A player id as an opaque string.

    FPL numbers its players, ESPN numbers its players, and MFL uses numeric-looking
    STRINGS — coercing with int() worked for all three right up until a platform used a
    non-numeric id, at which point the verifier would crash rather than report. Ids here
    are identifiers, not quantities, so they are compared as text and only ever against
    each other.
    """
    return str(value).strip()


def _by_element(picks: list[dict]) -> dict[str, dict]:
    return {_id(p["element"]): p for p in picks}


def verify_lineup(intended: list[dict], actual: list[dict], *,
                  platform: str = "fpl") -> VerifyResult:
    """Compare an intended pick list against what the platform reports afterwards.

    Checks the fields a lineup write actually sets — slot, captaincy, multiplier — and
    ignores everything the provider owns (prices, element_type), because a difference
    there is not evidence the write failed.

    `platform` gates the rules that are not universal. ESPN has no captain and no fixed
    squad size, so applying FPL's would produce a mismatch on every correct ESPN write.
    """
    want, got = _by_element(intended), _by_element(actual)
    mismatches: list[str] = []

    missing = sorted(set(want) - set(got))
    if missing:
        mismatches.append(f"players missing from the squad afterwards: {missing}")

    if platform in PARTIAL_LINEUP_READBACK:
        # Everything below compares slots, which this platform does not report. Stop
        # here and say so rather than inventing a verdict either way.
        if mismatches:
            return VerifyResult(
                False, mismatches,
                "players named in the lineup are not on the roster")
        return VerifyResult(
            True, [],
            f"all {len(want)} named starters are on the roster — but this platform does "
            "not report starting slots, so the lineup ITSELF was not verified. Check it "
            "before kickoff.")

    extra = sorted(set(got) - set(want))
    if extra:
        mismatches.append(f"unexpected players in the squad: {extra}")

    for element in sorted(set(want) & set(got)):
        w, g = want[element], got[element]
        for field_ in ("position", "is_captain", "is_vice_captain", "multiplier"):
            if field_ not in w:
                continue
            if w[field_] != g.get(field_):
                mismatches.append(
                    f"element {element}: {field_} is {g.get(field_)!r}, expected {w[field_]!r}"
                )

    # A lineup with no captain is a specific, expensive failure worth naming rather than
    # leaving the reader to infer it from a list of field diffs — on platforms that HAVE
    # captains. ESPN does not.
    if platform in HAS_CAPTAIN and not any(p.get("is_captain") for p in actual):
        mismatches.append("NO CAPTAIN is set — this costs the captain's doubled points")

    # FPL only, and only on a COMPLETE squad. On a partial pick list an XI count proves
    # nothing, and asserting it anyway makes the verifier cry wolf — the failure mode
    # that gets a real mismatch ignored later. ESPN's slot map is per sport AND per
    # league, so its league settings are the authority, never a constant in this file.
    if platform == "fpl" and len(actual) == SQUAD_SIZE:
        starters = [p for p in actual if int(p.get("position", 99)) <= XI_SIZE]
        if len(starters) != XI_SIZE:
            mismatches.append(f"{len(starters)} players in the XI, expected {XI_SIZE}")

    if mismatches:
        return VerifyResult(False, mismatches, "the lineup the platform reports differs from the one sent")
    return VerifyResult(True, [], f"lineup confirmed — {len(actual)} picks match what was sent")


def _moves(intended: list[dict]) -> tuple[set[str], set[str]]:
    """(coming in, going out) from either platform's move vocabulary.

    FPL sends {element_in, element_out}; ESPN sends items typed ADD or DROP against a
    playerId. Normalising here means one verifier, not two — and one place to be wrong.
    """
    ins: set[str] = set()
    outs: set[str] = set()
    for t in intended:
        if (v := t.get("element_in")) is not None:
            ins.add(_id(v))
        if (v := t.get("element_out")) is not None:
            outs.add(_id(v))
        kind = str(t.get("type") or "").upper()
        if (pid := t.get("playerId")) is not None:
            if kind == "ADD":
                ins.add(_id(pid))
            elif kind == "DROP":
                outs.add(_id(pid))
    return ins, outs


def verify_transfers(
    intended: list[dict], squad_before: list[dict], squad_after: list[dict], *,
    platform: str = "fpl",
) -> VerifyResult:
    """Confirm each intended transfer actually moved.

    Checked against the squad rather than the response body, because the response is
    undocumented and the squad is the thing the owner cares about.
    """
    before = {_id(p["element"]) for p in squad_before}
    after = {_id(p["element"]) for p in squad_after}
    mismatches: list[str] = []
    expected_in, expected_out = _moves(intended)

    for pid in sorted(expected_in):
        if pid not in after:
            mismatches.append(f"player {pid} was supposed to come IN and is not in the squad")
    for pid in sorted(expected_out):
        if pid in after:
            mismatches.append(f"player {pid} was supposed to go OUT and is still in the squad")

    # Anything that moved which nobody asked for is the scariest outcome — it means the
    # write did something other than what was approved.
    unexpected_in = (after - before) - expected_in
    unexpected_out = (before - after) - expected_out
    if unexpected_in:
        mismatches.append(f"players arrived that were NOT requested: {sorted(unexpected_in)}")
    if unexpected_out:
        mismatches.append(f"players left that were NOT requested: {sorted(unexpected_out)}")

    # FPL swaps are always one-for-one, so a size change is a red flag. ESPN adds and
    # drops need not pair (a roster with a spare slot takes an ADD alone), so the size
    # legitimately moves and only the NET is checkable.
    if platform == "fpl" and len(after) != len(before):
        mismatches.append(f"squad size changed from {len(before)} to {len(after)}")
    else:
        net = len(expected_in) - len(expected_out)
        if len(after) - len(before) != net:
            mismatches.append(
                f"roster went from {len(before)} to {len(after)}; the requested moves "
                f"account for {net:+d}")

    if mismatches:
        return VerifyResult(False, mismatches, "the squad after the transfer is not what was requested")
    return VerifyResult(True, [], f"{len(intended)} transfer(s) confirmed against the squad")


async def escalate(result: VerifyResult, channel: str | None = None) -> bool:
    """A failed verification pages; a successful one does not.

    Deliberately asymmetric. Successes are a weekly digest at most — a notification per
    confirmed lineup trains the owner to swipe them away, and the one that matters gets
    swiped with the rest.

    On ntfy the failure goes out at urgent priority, which is the difference between a
    badge and a phone that rings. This is the one fantasy notification worth waking
    someone for: their team is not what they approved and the deadline is coming.
    """
    if result.ok:
        return False
    from ..observability.notify import ntfy_url_for, post_ntfy, push_to_channel

    channel = channel or _alert_channel()
    text = result.as_notification()
    if channel == "ntfy" or channel.startswith("ntfy:"):
        return await post_ntfy(ntfy_url_for(channel) or "", text, priority="urgent")
    return await push_to_channel(channel, text)


def _alert_channel() -> str:
    from .approvals import alert_channel

    return alert_channel()
