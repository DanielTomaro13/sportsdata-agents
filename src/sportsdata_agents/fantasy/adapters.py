"""What differs between one fantasy platform and the next — and nothing else.

`execute.run_intent` used to name FPL's tools directly. That was right when FPL was the
only platform: a seam invented before a second case is a guess. ESPN is the second case,
and it shows exactly where the seam belongs — four questions, per platform:

    which tool writes a lineup?      which writes a roster move?
    how do I read the squad back?    what shape is a "pick"?

Everything else — policy, proposals, expiry, read-back, the refusal to retry — is already
platform-agnostic and stays where it is.

THE ONE THING ADAPTERS MAY NOT DO is decide whether a write happens. They translate an
approved intent into a request. If an adapter could veto or permit, there would be two
policies, and the one nobody was looking at would be the one that mattered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class Adapter(Protocol):
    platform: str

    def lineup_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]: ...
    def roster_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]: ...
    def read_squad_call(self, entry: int, ctx: dict) -> tuple[str, dict]: ...
    def picks_from(self, body: Any, ctx: dict) -> list[dict]: ...
    def intended_picks(self, payload: dict) -> list[dict]: ...


@dataclass
class FPLAdapter:
    """Fantasy Premier League. One team per manager id; the squad IS the response."""

    platform: str = "fpl"

    def lineup_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        return "fpl_set_lineup", {"managerId": entry, "csrf": ctx.get("csrf", ""), **payload}

    def roster_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        return "fpl_transfers", {"csrf": ctx.get("csrf", ""), **payload}

    def read_squad_call(self, entry: int, ctx: dict) -> tuple[str, dict]:
        return "fpl_my_team", {"managerId": entry}

    def picks_from(self, body: Any, ctx: dict) -> list[dict]:
        return list(body.get("picks") or []) if isinstance(body, dict) else []

    def intended_picks(self, payload: dict) -> list[dict]:
        return list(payload.get("picks") or [])


@dataclass
class ESPNAdapter:
    """ESPN Fantasy. A team is (league, season, game, teamId), so identity needs context
    FPL does not — and the read returns EVERY team, so ours must be picked out."""

    platform: str = "espn"
    #: Required context, named so a missing one fails with a sentence rather than a KeyError.
    needs: tuple[str, ...] = ("leagueId", "seasonId", "game")
    _: dict = field(default_factory=dict)

    def _base(self, ctx: dict) -> dict:
        missing = [k for k in self.needs if not ctx.get(k)]
        if missing:
            raise ValueError(
                f"ESPN needs {', '.join(missing)} to identify the team — these come from "
                "the league URL and are stored with the policy, not guessed."
            )
        return {"game": ctx["game"], "seasonId": int(ctx["seasonId"]),
                "leagueId": int(ctx["leagueId"])}

    def lineup_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        return "espnfantasy_set_lineup", {
            **self._base(ctx), "teamId": entry,
            "scoringPeriodId": ctx.get("scoringPeriodId"), **payload,
        }

    def roster_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        return "espnfantasy_add_drop", {
            **self._base(ctx), "teamId": entry,
            "scoringPeriodId": ctx.get("scoringPeriodId"), **payload,
        }

    def read_squad_call(self, entry: int, ctx: dict) -> tuple[str, dict]:
        return "espnfantasy_rosters", {**self._base(ctx), "view": ["mRoster"]}

    def picks_from(self, body: Any, ctx: dict) -> list[dict]:
        """Pull OUR team's roster out of a league-wide response, and normalise it to the
        same {element, position} shape the verifier already understands.

        `element` is the playerId and `position` is the lineup slot — so one verifier
        compares an FPL squad and an ESPN roster without knowing which it is looking at.
        A team id that is not in the response yields [], which the verifier reports as a
        mismatch: the safe direction.
        """
        if not isinstance(body, dict):
            return []
        entry = int(ctx.get("teamId") or 0)
        for team in body.get("teams") or []:
            if int(team.get("id", -1)) != entry:
                continue
            entries = ((team.get("roster") or {}).get("entries")) or []
            return [
                {"element": int(e["playerId"]), "position": e.get("lineupSlotId"),
                 "is_captain": False, "is_vice_captain": False, "multiplier": 1}
                for e in entries if e.get("playerId") is not None
            ]
        return []


    def intended_picks(self, payload: dict) -> list[dict]:
        """The lineup we asked for, in the shape `picks_from` returns.

        ESPN's write payload names each move by where it is GOING (`toLineupSlotId`),
        while the read reports where each player now IS (`lineupSlotId`). Normalising
        both to {element, position} is what lets one verifier compare them — and getting
        this wrong is silent: an empty intent matches nothing, so every write "fails".
        """
        return [
            {"element": int(i["playerId"]), "position": i.get("toLineupSlotId"),
             "is_captain": False, "is_vice_captain": False, "multiplier": 1}
            for i in (payload.get("items") or [])
            if i.get("playerId") is not None and str(i.get("type", "LINEUP")).upper() == "LINEUP"
        ]


@dataclass
class MFLAdapter:
    """MyFantasyLeague. A team is (year, league, franchise) — and unlike the others, the
    lineup write is a FULL REPLACEMENT expressed as a flat list of starter ids."""

    platform: str = "mfl"
    needs: tuple[str, ...] = ("leagueId", "year")

    def _base(self, ctx: dict) -> dict:
        missing = [k for k in self.needs if not ctx.get(k)]
        if missing:
            raise ValueError(
                f"MFL needs {', '.join(missing)} to identify the team — these come from "
                "the league URL and are stored with the policy, not guessed."
            )
        return {"year": int(ctx["year"]), "L": str(ctx["leagueId"])}

    def lineup_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        args = {**self._base(ctx), "W": ctx.get("week"), **payload}
        # FRANCHISE_ID means "act as another franchise" and is commissioner-only. It is
        # never sent from here: an agent acting on its owner's team is acting as itself,
        # and the one way to accidentally rewrite a stranger's lineup is to pass this.
        args.pop("FRANCHISE_ID", None)
        return "mfl_set_lineup", args

    def roster_call(self, entry: int, payload: dict, ctx: dict) -> tuple[str, dict]:
        tool = payload.pop("_tool", "mfl_add_drop")
        args = {**self._base(ctx), **payload}
        args.pop("FRANCHISE_ID", None)
        return tool, args

    def read_squad_call(self, entry: int, ctx: dict) -> tuple[str, dict]:
        return "mfl_rosters", {**self._base(ctx), "FRANCHISE": _franchise(entry)}

    def picks_from(self, body: Any, ctx: dict) -> list[dict]:
        """Our franchise's roster, normalised to {element, position}.

        `position` carries MFL's roster STATUS (ROSTER / INJURED_RESERVE / TAXI_SQUAD)
        rather than a lineup slot, because that is what MFL's roster read actually
        reports and what an IR move changes. Starters are not visible here at all —
        which is why a lineup write is verified against the league's own lineup read,
        not this one.
        """
        if not isinstance(body, dict):
            return []
        franchises = _as_list((body.get("rosters") or {}).get("franchise"))
        want = _franchise(ctx.get("teamId", ""))
        for fr in franchises:
            if str(fr.get("id")) != want:
                continue
            return [
                {"element": str(p["id"]), "position": p.get("status", "ROSTER"),
                 "is_captain": False, "is_vice_captain": False, "multiplier": 1}
                for p in _as_list(fr.get("player")) if p.get("id") is not None
            ]
        return []

    def intended_picks(self, payload: dict) -> list[dict]:
        """The starters we asked for, as bare ids.

        No `position` is set, because MFL's lineup write says nothing about roster status
        and its roster read says nothing about starting slots — the two do not share a
        field to compare. The verifier handles this by checking membership only (see
        PARTIAL_LINEUP_READBACK) and reporting that the slots went unverified.
        """
        return [{"element": str(pid)} for pid in (payload.get("STARTERS") or [])]


def _franchise(entry: int | str) -> str:
    """MFL franchise ids are four-digit strings: 1 -> '0001'. Passing the int silently
    matches nothing, which reads as "you are not in this league"."""
    return str(entry).zfill(4)


def _as_list(value: Any) -> list:
    """MFL returns one row as an object and many as a list. Everything that walks a
    response has to normalise, so it is done in one place."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


ADAPTERS: dict[str, Adapter] = {
    "fpl": FPLAdapter(),
    "espn": ESPNAdapter(),
    "mfl": MFLAdapter(),
}


def adapter_for(platform: str) -> Adapter:
    a = ADAPTERS.get(platform)
    if a is None:
        raise ValueError(f"no fantasy adapter for platform {platform!r}; have {sorted(ADAPTERS)}")
    return a
