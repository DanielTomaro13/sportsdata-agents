#!/usr/bin/env python3
"""Regenerate site/catalogue.json from the sportsdata-mcp specs.

    python3 scripts/gen-catalogue.py            # write site/catalogue.json
    python3 scripts/gen-catalogue.py --check    # exit 1 if it is out of date

The public site's provider browser is driven entirely by this file. It was previously
maintained by a generator that got lost, after which the catalogue simply stopped being
updated — by 2026-08-11 it advertised 29 providers and 522 tools against a real 60 and
738, and showed none of the bring-your-own-key tier at all. A site that undersells the
product by half is worse than no site, so the generator is back and `--check` exists to
make staleness a failure rather than a slow drift.

THE SPECS ARE THE ONLY SOURCE. Nothing here is hand-maintained: names, counts, argument
lists and the BYO flag are all read from the MCP provider specs, so adding a provider
there is the only step needed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "catalogue.json"

# Where to find the MCP package when it isn't installed in this environment.
MCP_REPO = ROOT.parent / "sportsdata-mcp" / "src"

# Provider "kind" drives the site's grouping and its geo-restriction warning. Bookmakers
# and prediction markets carry the warning; everything else is reference data.
GAMBLING = {
    "betfair", "betr", "dabble", "entain", "fanduel", "kalshi", "pinnacle", "pointsbet",
    "polymarket", "racingandsports", "sportsbet", "tab", "unibet",
    # BYO odds aggregators — same warning applies.
    "theoddsapi", "oddsapiio", "sportsgameodds", "isportsapi",
}
SOCIAL = {"twitter"}

# A finer split than `kind`, so the site's headline can count categories without anyone
# hand-tallying them. The hero line said "11 bookmakers and 2 prediction markets, plus 15
# sports feeds" for months after those numbers stopped being true.
PREDICTION_MARKETS = {"kalshi", "polymarket"}
ODDS_AGGREGATORS = {"theoddsapi", "oddsapiio", "sportsgameodds", "isportsapi"}
FORM_SERVICES = {"racingandsports"}


def _tag(pid: str, kind: str) -> str:
    """bookmaker | prediction | aggregator | form | sport | social."""
    if pid in PREDICTION_MARKETS:
        return "prediction"
    if pid in ODDS_AGGREGATORS:
        return "aggregator"
    if pid in FORM_SERVICES:
        return "form"
    if kind == "gambling":
        return "bookmaker"
    return kind

# Display names for providers whose spec display_name carries a parenthetical the site
# does not need ("(ATP/WTA/ITF — BYO key)" is already conveyed by the 🔑 badge).
def _display_name(provider) -> str:
    name = provider.display_name or provider.id
    return name.split(" (")[0].split(" — ")[0].strip()


def _kind(pid: str) -> str:
    if pid in GAMBLING:
        return "gambling"
    if pid in SOCIAL:
        return "social"
    return "sport"


def _example(tool) -> str:
    """A short shape sketch for the site's tool card.

    The old catalogue carried real recorded responses for 66 of 522 tools and nothing for
    the rest. `response_hint` is written for every endpoint and is what the model itself
    is told, so it is both more complete and guaranteed consistent with the server.
    """
    hint = (getattr(tool, "response_hint", None) or "").strip()
    if not hint:
        return ""
    # Strip our internal verification notes — they are for contributors, not visitors.
    for marker in ("  — SHAPE FROM VENDOR DOCS", " — SHAPE FROM VENDOR DOCS", "  — VERIFIED"):
        if marker in hint:
            hint = hint.split(marker)[0]
    return hint[:400].strip()


def build() -> dict:
    if MCP_REPO.exists() and str(MCP_REPO) not in sys.path:
        sys.path.insert(0, str(MCP_REPO))
    from sportsdata_mcp.spec_loader import load_all_specs  # noqa: PLC0415 - optional dep

    providers = []
    for spec in sorted(load_all_specs(), key=lambda s: s.provider.id):
        p = spec.provider
        tools = []
        for tool in sorted(spec.all_tools(), key=lambda t: t.name):
            tools.append(
                {
                    "name": tool.name,
                    "desc": (getattr(tool, "summary", "") or "").strip(),
                    "args": [
                        {"n": param.name, "t": param.type, "r": bool(param.required)}
                        for param in getattr(tool, "params", [])
                    ],
                    "example": _example(tool),
                }
            )
        providers.append(
            {
                "id": p.id,
                "name": _display_name(p),
                "kind": _kind(p.id),
                "tag": _tag(p.id, _kind(p.id)),
                "byo": bool(p.requires_user_key),
                "proxied": bool(getattr(p, "proxied", False)),
                "verified": bool(p.shapes_verified),
                "count": len(tools),
                "tools": tools,
            }
        )
    return {"providers": providers}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if catalogue.json is stale")
    args = ap.parse_args()

    data = build()
    rendered = json.dumps(data, indent=1) + "\n"
    n_prov = len(data["providers"])
    n_tools = sum(p["count"] for p in data["providers"])

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != rendered:
            have = json.loads(current)["providers"] if current else []
            print(
                f"catalogue.json is STALE: file has {len(have)} providers / "
                f"{sum(p['count'] for p in have)} tools, specs have {n_prov} / {n_tools}.\n"
                f"Run: python3 scripts/gen-catalogue.py",
                file=sys.stderr,
            )
            return 1
        print(f"catalogue.json is current ({n_prov} providers, {n_tools} tools)")
        return 0

    OUT.write_text(rendered)
    byo = sum(1 for p in data["providers"] if p["byo"])
    print(f"wrote {OUT.relative_to(ROOT)} — {n_prov} providers, {n_tools} tools, {byo} needing a user key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
