"""Guard the data-plane leverage: every capability the data plane publishes is either
granted to an agent or waived with a written reason, and the labels file matches upstream.

This replaced a `>= 30` floor. That floor passed while 31 of 68 capabilities were dark,
because it counted against `capability_labels.json` — a hand-maintained copy that had
itself fallen five entries behind, making the guard blind to exactly the tags that
mattered. A count cannot notice what it does not know exists, so the assertion is now
set equality against the live catalogue rather than a threshold.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.tools.builder import capability_labels

pytestmark = pytest.mark.unit

_AUDIT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "capability-audit.py"


def _audit_module():
    """Load scripts/capability-audit.py (a script, not an importable package)."""
    spec = importlib.util.spec_from_file_location("capability_audit", _AUDIT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit():
    """The live audit, or a skip when the data plane is not available to read.

    `sportsdata-mcp` ships its specs inside its wheel, so an installed copy is enough
    and CI (which installs it as a dev extra) runs this for real. A dev box with
    neither an install nor a sibling checkout skips rather than failing on setup.
    """
    if not _AUDIT.exists():
        pytest.skip("capability-audit.py not present")
    module = _audit_module()
    try:
        return module.audit()
    except module.CatalogueUnavailable as exc:
        pytest.skip(str(exc))


def _used_capabilities() -> set[str]:
    used: set[str] = set()
    for spec in load_builtin_specs().values():
        used.update(spec.tools.mcp_capabilities)
    return used


def test_racing_and_prediction_surfaces_are_leveraged() -> None:
    used = _used_capabilities()
    assert {c for c in used if c.startswith("racing.")}, "no agent uses any racing.* capability"
    assert {c for c in used if c.startswith("prediction.")}, "no agent uses any prediction.* capability"


def test_every_capability_is_wired_or_waived(audit) -> None:
    """The ledger must be complete. Silence is what let 31 capabilities go dark."""
    assert not audit["unwaived"], (
        f"{len(audit['unwaived'])} capability tag(s) neither granted to an agent nor waived: "
        f"{', '.join(audit['unwaived'])}. Grant them, or record why not in "
        f"docs/capability-waivers.yaml."
    )


def test_no_agent_references_a_capability_the_data_plane_dropped(audit) -> None:
    """A renamed or removed tag upstream currently surfaces as a runtime
    CapabilityResolutionError for whoever runs that agent. Fail the build instead.

    Skipped under version skew: when the INSTALLED data plane is older than the one this
    repo was built against, a tag missing from it has not been dropped — it has not been
    published yet. Same distinction the audit CLI makes; see docs/capability-waivers.yaml.
    """
    if audit["skew"]:
        pytest.skip(f"version skew — {audit['skew']}")
    assert not audit["undeclared"], (
        f"agent specs reference capability tag(s) the data plane no longer publishes: "
        f"{', '.join(audit['undeclared'])}"
    )


def test_capability_labels_match_the_data_plane(audit) -> None:
    """The labels file is generated. If it drifts, the guard above goes blind again.

    Skipped under skew for the same reason: the labels were generated from a newer
    catalogue than the one installed here, so a mismatch is the lag, not drift.
    """
    if audit["skew"]:
        pytest.skip(f"version skew — {audit['skew']}")
    assert not audit["labels_stale"], (
        "src/sportsdata_agents/agents/capability_labels.json no longer matches the data "
        "plane — run `python3 scripts/capability-audit.py --regenerate`"
    )


def test_every_offered_capability_has_a_display_label(audit) -> None:
    """`capability_labels.json` drives the agent-builder's picker; a blank label there
    is a blank row in front of a user."""
    assert not audit["unlabelled"], (
        f"no display label for: {', '.join(audit['unlabelled'])} — add one by hand "
        f"(the description comes from upstream, the label is ours)"
    )


def test_labels_offer_no_capability_that_resolves_to_nothing() -> None:
    """Upstream declares `sport.example` as a schema sample exposed by no tool. Offering
    it in the picker would be offering a dead end."""
    assert "sport.example" not in capability_labels()


def test_new_specialists_load_and_are_pro_only() -> None:
    from sportsdata_agents.licensing.entitlements import entitlements_for_tier

    specs = load_builtin_specs()
    for agent_id in ("racing_analyst", "prediction_market_analyst"):
        assert agent_id in specs, f"{agent_id} spec did not load"
        assert specs[agent_id].plane == "product"

    plus = entitlements_for_tier("plus")
    assert plus.agents is not None  # plus has a restricted roster
    assert "racing_analyst" not in plus.agents  # full roster is a Pro feature
    pro = entitlements_for_tier("pro")
    assert pro.allows_agent("racing_analyst") and pro.allows_agent("prediction_market_analyst")


def test_orchestrator_can_reach_the_new_specialists() -> None:
    orch = load_builtin_specs()["orchestrator"]
    assert "racing_analyst" in orch.can_delegate_to
    assert "prediction_market_analyst" in orch.can_delegate_to


def test_the_live_plane_is_wired() -> None:
    """The platform's largest blind spot, closed. Everything else here is pre-game or
    post-game; `sport.in_play` (20 providers) and `sport.match_score` (36) were granted
    by no agent at all, so between the first whistle and the last the desk saw nothing.
    """
    specs = load_builtin_specs()
    assert "live_desk" in specs, "the live desk spec did not load"
    live = specs["live_desk"]
    reach = set(live.tools.mcp_capabilities) | set(live.tools.mcp_discover)
    assert {"sport.in_play", "sport.match_score"} <= reach


def test_a_single_provider_capability_is_never_carried() -> None:
    """Carrying one is a startup landmine: `bridge_mcp_tools` treats a capability that
    resolves to zero tools as a spec error, so if that provider is disabled — a toggle
    any operator can flip — the agent refuses to start. Discovery degrades instead, so
    single-provider capabilities belong in `mcp_discover`.

    Guards the specific bug this caught: live_desk carried `sport.cash_out`, whose only
    tool is betfair_cashout, and would not start with Betfair off.
    """
    single_provider = {
        "sport.cash_out", "sport.depth_chart", "stats.shot_chart", "content.photo",
        "racing.price_history", "racing.track_conditions", "social.post_search",
        "social.post_detail", "social.user_profile", "social.user_timeline",
        "social.trends",
    }
    offenders = [
        f"{agent_id}: {sorted(set(spec.tools.mcp_capabilities) & single_provider)}"
        for agent_id, spec in load_builtin_specs().items()
        if set(spec.tools.mcp_capabilities) & single_provider
    ]
    assert not offenders, (
        "single-provider capabilities must be discovered, not carried — a disabled "
        f"provider would stop these agents starting: {offenders}"
    )


def test_a_group_only_agent_scopes_narrowly() -> None:
    """`runtime` passes `mcp_capabilities or None` to the bridge, and None means NO
    FILTER — so an agent granting `mcp_groups` with no capabilities receives every tool
    in those groups, whatever they contain.

    That is safe today only because the four fantasy managers name tight, specific groups
    (`fpl.players`, `sleeper.draft`). It stops being safe the moment someone grants a
    wildcard or a whole provider, which would silently hand an agent the entire scope.
    The behaviour is deliberate and documented; this pins the assumption it rests on.
    """
    offenders: list[str] = []
    for agent_id, spec in load_builtin_specs().items():
        if spec.tools.mcp_capabilities or not spec.tools.mcp_groups:
            continue  # a capability list makes the bridge filter again
        for group in spec.tools.mcp_groups:
            # A group id is "<provider>.<area>". A bare provider or a wildcard is the
            # whole book, and unfiltered is far too much of it.
            if "*" in group or "." not in group:
                offenders.append(f"{agent_id}: {group!r}")
    assert not offenders, (
        "these agents grant broad groups with no capability filter, so they receive the "
        f"entire scope unfiltered: {offenders}"
    )


def test_fantasy_is_reachable_across_every_platform_not_just_the_ones_with_agents() -> None:
    """The point of phase 4. `fantasy.*` spans ESPN, FPL, MyFantasyLeague, Sleeper,
    Yahoo and SuperCoach — the last two have no manager agent, and before this nothing
    could read them at all: the four managers take the mcp_groups door, which is
    per-platform by construction.
    """
    advisor = load_builtin_specs()["fantasy_advisor"]
    reach = set(advisor.tools.mcp_capabilities) | set(advisor.tools.mcp_discover)
    assert {"fantasy.rosters", "fantasy.league_settings"} <= reach


def test_nothing_is_waived_as_merely_planned_any_more() -> None:
    """Phase 5 was the last of the planned wiring. What remains waived should be waived
    on its merits — `niche`, `redundant`, or an `upstream` placeholder — not queued.

    The one exception is stats.closing_odds, which moved to phase 7 when it turned out
    not to be an agent grant at all: CLV is benchmarked in quant/backtest.py against
    Price rows in the warehouse, so a capability an agent can call ad hoc feeds nothing.
    It needs the closes INGESTED, which is pipeline work.
    """
    import yaml

    waivers = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "docs" / "capability-waivers.yaml").read_text()
    )["waivers"]
    still_queued = sorted(
        cap for cap, entry in waivers.items()
        if entry.get("status") == "planned" and cap != "stats.closing_odds"
    )
    assert not still_queued, f"planned-but-unwired capabilities remain: {still_queued}"
