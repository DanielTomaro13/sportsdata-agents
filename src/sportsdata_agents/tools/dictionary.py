"""Market-dictionary steward tools (resolution milestone) — the dictionary is DATA.

The packaged seed ships with the code; the steward maintains a LOCAL OVERRIDES file
(``SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES``, default ``market_dictionary.local.json``)
so extending canonicalization never needs a code change. Merge safety is enforced in
CODE, not just in the prompt: qualifier names (halves, quarters, overtime-only,
alternates) can never alias into the base families — different settlement rules are
different markets, and a wrong merge manufactures phantom edges.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import Price
from sportsdata_agents.operations.ingestion.normalizers import (
    canonical_market,
    reload_dictionary,
)

DICTIONARY_TOOL_NAMES = {
    "list_market_names",
    "get_market_dictionary",
    "add_market_alias",
    "remove_market_alias",
}

# Names carrying these tokens settle differently from the base market — alias-ing
# them into h2h/spread/total would merge different bets. Enforced, not advisory.
_QUALIFIER_TOKENS = (
    "1st", "2nd", "3rd", "4th", "first", "second", "third", "fourth",
    "half", "quarter", "period", "overtime", "extra time", " alt", "alternate",
    "p1", "p2", "p3", "p4",
)
_BASE_FAMILIES = ("h2h", "spread", "total", "win", "place")


def _overrides_path() -> str:
    return os.environ.get("SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", "market_dictionary.local.json")


def _read_overrides() -> dict[str, Any]:
    path = _overrides_path()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"markets": {}, "sports": {}}


def _write_overrides(data: dict[str, Any]) -> None:
    data["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    with open(_overrides_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    reload_dictionary()


def dictionary_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[ToolDef]:
    async def list_market_names(args: dict[str, Any]) -> Any:
        """{only_unmapped?: bool, min_count?: int, limit?: int} → distinct market names
        in the warehouse with row counts and providers; unmapped = not yet a family."""
        only_unmapped = bool(args.get("only_unmapped", True))
        min_count = int(args.get("min_count", 20))
        limit = min(int(args.get("limit", 40)), 80)
        async with session_factory() as session:
            # provider lists aggregate in Python — string aggregation differs per
            # dialect (SQLite group_concat vs Postgres string_agg)
            rows = (
                await session.execute(
                    select(Price.market, Price.provider, func.count().label("n"))
                    .group_by(Price.market, Price.provider)
                )
            ).all()
        by_market: dict[str, dict[str, Any]] = {}
        for market, provider, n in rows:
            entry = by_market.setdefault(market, {"rows": 0, "providers": set()})
            entry["rows"] += n
            entry["providers"].add(provider)
        out = []
        for market, entry in sorted(by_market.items(), key=lambda kv: -kv[1]["rows"]):
            if entry["rows"] < min_count:
                continue
            family = canonical_market(market)
            mapped = market in _BASE_FAMILIES or family != market
            if only_unmapped and mapped:
                continue
            out.append({"market": market, "rows": entry["rows"],
                        "providers": ",".join(sorted(entry["providers"])),
                        "currently": family if mapped else "unmapped"})
            if len(out) >= limit:
                break
        return {"names": out, "note": "qualifier markets (halves/quarters/overtime/alt) "
                                      "must NOT be aliased into base families"}

    async def get_market_dictionary(args: dict[str, Any]) -> Any:
        """The current dictionary: packaged seed families + local overrides."""
        from importlib import resources

        seed = json.loads(
            resources.files("sportsdata_agents.operations.resolution")
            .joinpath("market_dictionary.json")
            .read_text(encoding="utf-8")
        )
        return {"seed": {k: v for k, v in seed.items() if not k.startswith("_")},
                "overrides": _read_overrides(), "overrides_path": _overrides_path()}

    async def add_market_alias(args: dict[str, Any]) -> Any:
        """{section: markets|sports, family, alias, rationale} → add an alias to the
        LOCAL overrides (refused when it would merge rule-different markets)."""
        section = str(args.get("section", "markets"))
        if section not in ("markets", "sports"):
            raise ValueError("section must be markets or sports")
        family = " ".join(str(args["family"]).strip().lower().split())
        alias = " ".join(str(args["alias"]).strip().lower().split())
        if not family or not alias or family == alias:
            raise ValueError("family and alias must be non-empty and different")
        if section == "markets":
            # qualifier tokens in the ALIAS must appear in the FAMILY name too —
            # "spread p1 alt" may not merge into "spread alt" (different period),
            # let alone into "spread". Applies to steward-created families as well.
            mismatched = [t for t in _QUALIFIER_TOKENS if t in alias and t not in family]
            if mismatched:
                raise ValueError(
                    f"refused: {alias!r} carries the qualifier(s) {mismatched} that "
                    f"{family!r} does not — qualifier markets settle differently; give "
                    f"it a family naming the qualifier (e.g. '{family} {mismatched[0]}')"
                )
        current = canonical_market(alias) if section == "markets" else None
        if section == "markets" and current not in (alias, family):
            raise ValueError(f"refused: {alias!r} is already mapped to {current!r}")
        data = _read_overrides()
        data.setdefault(section, {}).setdefault(family, [])
        if alias in data[section][family]:
            return {"added": False, "note": "alias already present"}
        data[section][family].append(alias)
        data.setdefault("rationales", {})[f"{section}:{alias}"] = str(args.get("rationale", ""))
        _write_overrides(data)
        return {"added": True, "section": section, "family": family, "alias": alias}

    async def remove_market_alias(args: dict[str, Any]) -> Any:
        """{alias, section?} → remove an alias from the LOCAL overrides (seed entries
        are code-shipped and reported, not removable here)."""
        alias = " ".join(str(args["alias"]).strip().lower().split())
        section = str(args.get("section", "markets"))
        data = _read_overrides()
        for family, aliases in (data.get(section) or {}).items():
            if alias in aliases:
                aliases.remove(alias)
                _write_overrides(data)
                return {"removed": True, "family": family}
        return {"removed": False,
                "note": "not in overrides — if it maps, it's a seed entry (edit the repo)"}

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("list_market_names", list_market_names,
              {"only_unmapped": {"type": "boolean"}, "min_count": {"type": "integer"},
               "limit": {"type": "integer"}}, []),
        _tool("get_market_dictionary", get_market_dictionary, {}, []),
        _tool("add_market_alias", add_market_alias,
              {"section": {"type": "string", "enum": ["markets", "sports"]},
               "family": {"type": "string"}, "alias": {"type": "string"},
               "rationale": {"type": "string"}},
              ["family", "alias"]),
        _tool("remove_market_alias", remove_market_alias,
              {"alias": {"type": "string"}, "section": {"type": "string"}}, ["alias"]),
    ]
