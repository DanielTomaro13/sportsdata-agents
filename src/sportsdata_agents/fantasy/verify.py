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


def _by_element(picks: list[dict]) -> dict[int, dict]:
    return {int(p["element"]): p for p in picks}


def verify_lineup(intended: list[dict], actual: list[dict]) -> VerifyResult:
    """Compare an intended pick list against what FPL reports afterwards.

    Checks the fields a lineup write actually sets — slot, captaincy, multiplier — and
    ignores everything the provider owns (prices, element_type), because a difference
    there is not evidence the write failed.
    """
    want, got = _by_element(intended), _by_element(actual)
    mismatches: list[str] = []

    missing = sorted(set(want) - set(got))
    if missing:
        mismatches.append(f"players missing from the squad afterwards: {missing}")
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
    # leaving the reader to infer it from a list of field diffs.
    if not any(p.get("is_captain") for p in actual):
        mismatches.append("NO CAPTAIN is set — this costs the captain's doubled points")

    # Only meaningful on a complete FPL squad. On a partial pick list — a lineup write
    # that touches a few slots — an XI count proves nothing, and asserting it anyway
    # would make the verifier cry wolf, which is the failure mode that gets a real
    # mismatch ignored later.
    if len(actual) == SQUAD_SIZE:
        starters = [p for p in actual if int(p.get("position", 99)) <= XI_SIZE]
        if len(starters) != XI_SIZE:
            mismatches.append(f"{len(starters)} players in the XI, expected {XI_SIZE}")

    if mismatches:
        return VerifyResult(False, mismatches, "the lineup FPL reports differs from the one sent")
    return VerifyResult(True, [], f"lineup confirmed — {len(actual)} picks match what was sent")


def verify_transfers(
    intended: list[dict], squad_before: list[dict], squad_after: list[dict]
) -> VerifyResult:
    """Confirm each intended transfer actually moved.

    Checked against the squad rather than the response body, because the response is
    undocumented and the squad is the thing the owner cares about.
    """
    before, after = {int(p["element"]) for p in squad_before}, {int(p["element"]) for p in squad_after}
    mismatches: list[str] = []

    for t in intended:
        in_, out_ = t.get("element_in"), t.get("element_out")
        if in_ is not None and int(in_) not in after:
            mismatches.append(f"element {in_} was supposed to come IN and is not in the squad")
        if out_ is not None and int(out_) in after:
            mismatches.append(f"element {out_} was supposed to go OUT and is still in the squad")

    # Anything that moved which nobody asked for is the scariest outcome — it means the
    # write did something other than what was approved.
    expected_in = {int(t["element_in"]) for t in intended if t.get("element_in") is not None}
    expected_out = {int(t["element_out"]) for t in intended if t.get("element_out") is not None}
    unexpected_in = (after - before) - expected_in
    unexpected_out = (before - after) - expected_out
    if unexpected_in:
        mismatches.append(f"players arrived that were NOT requested: {sorted(unexpected_in)}")
    if unexpected_out:
        mismatches.append(f"players left that were NOT requested: {sorted(unexpected_out)}")

    if len(after) != len(before):
        mismatches.append(f"squad size changed from {len(before)} to {len(after)}")

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
