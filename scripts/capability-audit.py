#!/usr/bin/env python3
"""Audit the agents' reach into the data plane's capability catalogue.

    python3 scripts/capability-audit.py              # print the ledger
    python3 scripts/capability-audit.py --check      # exit 1 on unexplained drift
    python3 scripts/capability-audit.py --regenerate # rewrite capability_labels.json

WHY THIS EXISTS. Two gaps accumulated in this repo for the same reason: the data plane
can grow in ways nothing forces anyone to notice. Capabilities were added upstream and
no agent ever granted them (31 of 68 were dark), and `capability_labels.json` — a
HAND-MAINTAINED copy of the upstream catalogue — silently fell five entries behind, so
the coverage test that intersects against it was structurally blind to exactly the tags
that mattered. Backfilling either one without a gate just restarts the same drift.

So: the upstream catalogue is the ONLY source for which capabilities exist. Everything
here is derived from it. A capability may be unwired, but only with a written reason in
`docs/capability-waivers.yaml` — silence is what let 31 of them disappear.

WHERE THE CATALOGUE COMES FROM. `sportsdata-mcp` is deliberately not a runtime
dependency (it is spawned as a subprocess), but its specs ship inside its wheel and load
via importlib.resources. So an installed copy is enough — no sibling checkout, which is
what lets `--check` run in CI rather than only on the operator machine. A sibling
checkout is the fallback for a dev box without it installed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LABELS = ROOT / "src" / "sportsdata_agents" / "agents" / "capability_labels.json"
WAIVERS = ROOT / "docs" / "capability-waivers.yaml"
SPECS = ROOT / "src" / "sportsdata_agents" / "specs"

#: Fallback when sportsdata-mcp is not installed in this environment (dev boxes that
#: work from two checkouts). Installed-first is what makes CI work.
MCP_REPO = ROOT.parent / "sportsdata-mcp" / "src"


class CatalogueUnavailable(RuntimeError):
    """Neither an installed sportsdata-mcp nor a sibling checkout was found."""


def load_catalogue() -> tuple[dict[str, str], dict[str, set[str]], dict[str, set[str]], int, int]:
    """(capability -> description, capability -> tools, group -> tools, total, untagged).

    Prefers the installed package so this runs in CI; falls back to a sibling checkout.
    Groups matter because an agent can grant `mcp_groups` INSTEAD of capabilities, and
    when it does the bridge attaches every tool in scope unfiltered — a second door that
    a capability-only count misses entirely.
    """
    try:
        import sportsdata_mcp  # noqa: F401
    except ImportError:
        if not MCP_REPO.exists():
            raise CatalogueUnavailable(
                "sportsdata-mcp is neither installed nor checked out beside this repo — "
                "`pip install sportsdata-mcp` (it is on PyPI) or clone it as a sibling"
            ) from None
        sys.path.insert(0, str(MCP_REPO))

    from sportsdata_mcp.spec_loader import load_all_specs, load_capabilities

    declared = {c.id: c.description for c in load_capabilities().capabilities}

    by_cap: dict[str, set[str]] = {}
    by_group: dict[str, set[str]] = {}
    total = untagged = 0
    for spec in load_all_specs():
        for tool in spec.all_tools():
            total += 1
            tags = getattr(tool, "capabilities", None) or []
            if not tags:
                untagged += 1
            for tag in tags:
                by_cap.setdefault(tag, set()).add(tool.name)
            group = getattr(tool, "group", None)
            if group:
                by_group.setdefault(str(group), set()).add(tool.name)
    return declared, by_cap, by_group, total, untagged


def catalogue_source() -> str:
    """Which data plane this run read — an installed wheel or a sibling checkout.

    They diverge routinely: the local checkout runs ahead of the last release, so CI
    (installed) and a dev box (checkout) can legitimately see different catalogues.
    Naming the source turns a baffling failure into an obvious one.
    """
    try:
        import sportsdata_mcp
    except ImportError:
        return f"sibling checkout {MCP_REPO}"
    version = getattr(sportsdata_mcp, "__version__", None)
    if version is None:
        try:
            from importlib.metadata import version as _v

            version = _v("sportsdata-mcp")
        except Exception:  # pragma: no cover - metadata absent in odd installs
            version = "unknown"
    path = pathlib.Path(sportsdata_mcp.__file__).resolve()
    kind = "sibling checkout" if MCP_REPO in path.parents else "installed"
    return f"sportsdata-mcp {version} ({kind})"


def wired_capabilities() -> tuple[dict[str, list[str]], set[str]]:
    """(capability -> granting agents, granted group ids). Specs read as data."""
    import yaml

    granted: dict[str, list[str]] = {}
    groups: set[str] = set()
    for path in sorted(SPECS.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        agent = (yaml.safe_load(path.read_text()) or {}).get("agent", {})
        tools = agent.get("tools", {}) or {}
        for cap in tools.get("mcp_capabilities") or []:
            granted.setdefault(cap, []).append(agent.get("id", path.stem))
        groups.update(tools.get("mcp_groups") or [])
    return granted, groups


def load_waivers() -> dict[str, dict]:
    if not WAIVERS.exists():
        return {}
    import yaml

    raw = yaml.safe_load(WAIVERS.read_text()) or {}
    return raw.get("waivers", {}) or {}


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def skew() -> str | None:
    """Non-None when this repo is AHEAD of the installed (released) data plane.

    Capabilities land in sportsdata-mcp before it is published, and agents CI installs
    the released wheel. A tag that exists locally but not in that wheel is therefore not
    drift — it is a normal, self-resolving lag between two repos that version
    independently. Conflating the two would make every capability addition red the build
    until an unrelated release happened, which teaches people to ignore the gate.
    """
    if not WAIVERS.exists():
        return None
    import yaml

    built_against = (yaml.safe_load(WAIVERS.read_text()) or {}).get("generated_from")
    if not built_against:
        return None
    try:
        from importlib.metadata import version as _v

        installed = _v("sportsdata-mcp")
    except Exception:
        return None
    if _version_tuple(installed) < _version_tuple(str(built_against)):
        return (
            f"this repo was built against sportsdata-mcp {built_against}, but "
            f"{installed} is installed — capabilities added upstream and not yet "
            f"published are reported, not failed"
        )
    return None


def merged_labels(declared: dict[str, str], by_cap: dict[str, set[str]]) -> dict[str, dict[str, str]]:
    """Upstream ids + descriptions, with this repo's hand-written labels preserved.

    The `label` is ours — short display text for the agent-builder's capability picker,
    which upstream has no notion of. The id set and the descriptions are upstream's.
    A capability with no label yet is reported, not invented.

    Capabilities that no tool actually exposes are left out: this file drives a picker
    users choose from, and offering a tag that resolves to zero tools is offering a
    dead end (upstream declares `sport.example` as a schema sample, not a real feed).
    """
    existing = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    out: dict[str, dict[str, str]] = {}
    for cap_id in sorted(declared):
        if not by_cap.get(cap_id):
            continue
        out[cap_id] = {
            "label": (existing.get(cap_id) or {}).get("label", ""),
            "description": declared[cap_id],
        }
    return out


def audit() -> dict:
    declared, by_cap, by_group, total_tools, untagged = load_catalogue()
    granted, granted_groups = wired_capabilities()
    waivers = load_waivers()

    wired = {c for c in granted if c in declared}
    unwired = sorted(set(declared) - wired)
    waived = {c for c in unwired if c in waivers}

    reachable: set[str] = set()
    for cap in wired:
        reachable |= by_cap.get(cap, set())
    # The second door: a group-scoped agent reaches every tool in that group, tagged or
    # not. Counting only capabilities understates reach and hides the group path.
    via_groups: set[str] = set()
    for group in granted_groups:
        via_groups |= by_group.get(group, set())
    reachable |= via_groups

    return {
        "source": catalogue_source(),
        "skew": skew(),
        "declared": declared,
        "by_cap": by_cap,
        "granted": granted,
        "waivers": waivers,
        "wired": sorted(wired),
        "unwired": unwired,
        "unwaived": sorted(set(unwired) - waived),
        # A spec naming a capability the data plane no longer publishes is a rename or a
        # removal upstream. Today that surfaces as a runtime CapabilityResolutionError
        # for whoever runs that agent; here it is a build failure instead.
        "undeclared": sorted(set(granted) - set(declared)),
        "unlabelled": sorted(c for c, v in merged_labels(declared, by_cap).items() if not v["label"]),
        "labels_stale": merged_labels(declared, by_cap)
        != (json.loads(LABELS.read_text()) if LABELS.exists() else {}),
        "total_tools": total_tools,
        "untagged_tools": untagged,
        "reachable_tools": len(reachable),
        "reachable_via_groups_only": len(via_groups - {t for c in wired for t in by_cap.get(c, set())}),
        "unreachable_tools": total_tools - len(reachable),
    }


def render(a: dict) -> None:
    d, w = a["declared"], a["waivers"]
    print(f"data plane              {a['source']}")
    if a["skew"]:
        print(f"  version skew          {a['skew']}")
    print(f"capabilities declared   {len(d)}")
    print(f"  wired                 {len(a['wired'])}")
    print(f"  unwired               {len(a['unwired'])}  ({len(a['unwired']) - len(a['unwaived'])} waived)")
    print(f"tools                   {a['total_tools']}")
    print(f"  reachable by an agent {a['reachable_tools']}  ({a['reachable_via_groups_only']} only via mcp_groups)")
    print(f"  unreachable           {a['unreachable_tools']}")
    print(f"  untagged upstream     {a['untagged_tools']}")
    print()
    if a["unwired"]:
        print("UNWIRED")
        for cap in a["unwired"]:
            entry = w.get(cap)
            n = len({t.split("_")[0] for t in a["by_cap"].get(cap, set())})
            if entry:
                print(f"  {cap:30} {n:3} prov  [{entry.get('status', '?')}] {entry.get('reason', '')[:58]}")
            else:
                print(f"  {cap:30} {n:3} prov  ** NO WAIVER **")
    if a["undeclared"]:
        print()
        print("REFERENCED BUT NOT DECLARED UPSTREAM (rename or removal):")
        for cap in a["undeclared"]:
            print(f"  {cap:30} granted by {', '.join(a['granted'][cap])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 on unexplained drift")
    ap.add_argument("--regenerate", action="store_true", help="rewrite capability_labels.json")
    args = ap.parse_args()

    try:
        a = audit()
    except CatalogueUnavailable as exc:
        print(f"capability-audit: {exc}", file=sys.stderr)
        return 0 if not args.check else 3

    if args.regenerate:
        merged = merged_labels(a["declared"], a["by_cap"])
        LABELS.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        print(f"wrote {LABELS.relative_to(ROOT)} ({len(merged)} capabilities)")
        if a["unlabelled"]:
            print("  no display label yet (add one by hand):", ", ".join(a["unlabelled"]))
        return 0

    render(a)

    if not args.check:
        return 0

    problems: list[str] = []
    # Under skew the installed catalogue is a SUBSET of what this repo was built for, so
    # "missing upstream" and "labels do not match" are both expected. Only the ledger
    # check — is every capability accounted for — still means anything.
    if a["skew"]:
        print()
        print(f"SKEW: {a['skew']}", file=sys.stderr)
    if a["undeclared"] and not a["skew"]:
        problems.append(
            f"{len(a['undeclared'])} capability tag(s) referenced by agent specs are not "
            f"published by {a['source']}: {', '.join(a['undeclared'])}. Either upstream renamed "
            f"or dropped them, or they are only in an unreleased data plane and this run read "
            f"the released one."
        )
    if a["unwaived"]:
        problems.append(
            f"{len(a['unwaived'])} capability tag(s) are neither wired nor waived: "
            f"{', '.join(a['unwaived'])} — grant them to an agent, or record why not in "
            f"{WAIVERS.relative_to(ROOT)}"
        )
    if a["labels_stale"] and not a["skew"]:
        problems.append(
            f"{LABELS.relative_to(ROOT)} no longer matches the data plane — "
            f"run `python3 scripts/capability-audit.py --regenerate`"
        )
    if problems:
        print()
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1
    print()
    print("OK — every capability is wired or waived, and the labels match upstream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
