"""Entitlement enforcement helpers — applied at the existing seams.

Enforcement is deterministic and lives in infrastructure (never in prompts):
the team roster is filtered, the MCP group list is capped, and channel/app
features are gated, all against :class:`Entitlements`. Ops-plane agents are
NEVER subject to entitlements — they're the platform's own maintenance team,
not a customer feature.
"""

from __future__ import annotations

from .entitlements import Entitlements, current_entitlements


class EntitlementError(RuntimeError):
    """A feature was used that the current license tier doesn't include."""

    def __init__(self, feature: str, need: str) -> None:
        super().__init__(f"{feature} requires {need}. See PRICING.md or run `agents license`.")
        self.feature = feature
        self.need = need


def cap_mcp_groups(groups: list[str], ents: Entitlements | None = None) -> list[str]:
    """Trim an MCP group list to the tier's quota (stable order, dedup).
    Unlimited (-1) passes through. The cap is a ceiling, not a denial — the
    user keeps the first N they configured."""
    ents = ents or current_entitlements()
    quota = ents.effective_mcp_quota()
    if quota < 0:
        return groups
    seen: list[str] = []
    for g in groups:
        if g not in seen:
            seen.append(g)
    return seen[:quota]


def filter_roster(specs: dict, root_id: str, ents: Entitlements | None = None) -> dict:
    """Keep ops agents and entitled PRODUCT agents; drop the rest and prune any
    now-dangling ``can_delegate_to`` so open_team still wires cleanly."""
    ents = ents or current_entitlements()
    if ents.agents is None:  # pro / unrestricted
        return specs
    allowed = set(ents.agents) | {root_id}
    kept = {
        sid: spec
        for sid, spec in specs.items()
        if getattr(spec, "plane", "product") == "ops" or sid in allowed
    }
    # prune delegate references to dropped agents (a frozen spec → rebuild the tuple)
    for sid, spec in list(kept.items()):
        cdt = getattr(spec, "can_delegate_to", None)
        if cdt:
            pruned = [d for d in cdt if d in kept]
            if len(pruned) != len(cdt):
                kept[sid] = spec.model_copy(update={"can_delegate_to": pruned})
    return kept


def require_addon(name: str, ents: Entitlements | None = None) -> None:
    ents = ents or current_entitlements()
    if not ents.has_addon(name):
        raise EntitlementError(f"the {name} integration", f"the '{name}' add-on")


def require_chat_ui(ents: Entitlements | None = None) -> None:
    ents = ents or current_entitlements()
    if not ents.chat_ui:
        raise EntitlementError("the chat interface", "the Plus tier or higher")


def require_full_app(ents: Entitlements | None = None) -> None:
    ents = ents or current_entitlements()
    if not ents.full_app:
        raise EntitlementError("the desktop app (agents + conductor)", "the Pro tier")
