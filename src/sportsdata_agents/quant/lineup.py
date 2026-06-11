"""DFS lineup optimisation (M3.3) — deterministic, no LLM in the math.

Beam search over roster slots: players sorted for determinism, the beam keeps the
top partial lineups by projection at every slot, and multi-position players are
considered for every slot they fit. Near-optimal at realistic sizes (≤ a few
hundred players, ≤ 10 slots) and exact on small instances; the LLM's job is the
inputs (projections, stacking preferences), never the arithmetic (P8).
"""

from __future__ import annotations

from typing import Any

BEAM_WIDTH = 2000


def optimize_lineup(
    players: list[dict[str, Any]],
    slots: list[str],
    salary_cap: float,
    *,
    locked: list[str] | None = None,
    excluded: list[str] | None = None,
    beam_width: int = BEAM_WIDTH,
) -> dict[str, Any]:
    """players: [{name, positions: [..] | position: str, salary, projection}];
    slots: e.g. ["PG","SG","SF","PF","C","G","F","UTIL"]; returns the best lineup
    found, its salary and projected points. Slot eligibility: a player fits a slot
    when the slot name is in their positions, or the slot is "UTIL"/"FLEX"."""
    locked_set = {str(n).lower() for n in (locked or [])}
    excluded_set = {str(n).lower() for n in (excluded or [])}
    pool: list[dict[str, Any]] = []
    for p in players:
        name = str(p.get("name", "")).strip()
        if not name or name.lower() in excluded_set:
            continue
        positions = p.get("positions") or ([p["position"]] if p.get("position") else [])
        pool.append({
            "name": name,
            "positions": {str(pos).upper() for pos in positions},
            "salary": float(p.get("salary", 0)),
            "projection": float(p.get("projection", 0)),
        })
    if not pool:
        raise ValueError("no eligible players")
    unknown_locks = locked_set - {p["name"].lower() for p in pool}
    if unknown_locks:
        raise ValueError(f"locked players not in the pool: {sorted(unknown_locks)}")
    # determinism: a stable order regardless of input order
    pool.sort(key=lambda p: (-p["projection"], p["salary"], p["name"]))

    def fits(player: dict[str, Any], slot: str) -> bool:
        slot = slot.upper()
        if slot in ("UTIL", "FLEX", "ANY"):
            return True
        if slot == "G":
            return bool({"PG", "SG", "G"} & player["positions"])
        if slot == "F":
            return bool({"SF", "PF", "F"} & player["positions"])
        return slot in player["positions"]

    # fill scarcer slots first — fewer eligible players = decide early
    slot_order = sorted(
        range(len(slots)),
        key=lambda i: sum(1 for p in pool if fits(p, slots[i])),
    )

    # beam state: (projection, salary, names frozenset, picks dict)
    beam: list[tuple[float, float, frozenset[str], dict[str, str]]] = [
        (0.0, 0.0, frozenset(), {})
    ]
    for slot_index in slot_order:
        slot = slots[slot_index]
        nxt: list[tuple[float, float, frozenset[str], dict[str, str]]] = []
        for proj, salary, used, picks in beam:
            for player in pool:
                if player["name"] in used or not fits(player, slot):
                    continue
                new_salary = salary + player["salary"]
                if new_salary > salary_cap:
                    continue
                nxt.append((
                    proj + player["projection"], new_salary,
                    used | {player["name"]},
                    {**picks, f"{slot_index}:{slot}": player["name"]},
                ))
        if not nxt:
            raise ValueError(f"no affordable player fits slot {slot!r}")
        nxt.sort(key=lambda s: (-s[0], s[1], sorted(s[2])))
        beam = nxt[:beam_width]

    # locked players: best lineup that contains all of them
    for proj, salary, used, picks in beam:
        if locked_set <= {n.lower() for n in used}:
            lineup = [
                {"slot": key.split(":", 1)[1], "name": picks[key]}
                for key in sorted(picks, key=lambda k: int(k.split(":", 1)[0]))
            ]
            return {
                "lineup": lineup,
                "salary": round(salary, 2),
                "salary_cap": salary_cap,
                "projected_points": round(proj, 2),
                "note": "beam search — deterministic, near-optimal; exact on small pools",
            }
    raise ValueError("no lineup under the cap contains every locked player")
