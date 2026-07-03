"""Tiers, add-ons, and the entitlement set a license grants."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

Tier = Literal["free", "base", "plus", "pro"]

# ── add-ons (paid toggles on top of a tier) ──────────────────────────────────
# Each is a capability the license can switch on independently of tier.
ADDONS: dict[str, str] = {
    "slack": "Slack integration — chat with the team and receive alerts in Slack",
    "discord": "Discord integration — chat and alerts in Discord",
    "premium_data": "Premium data providers (DataGolf, Betfair authed) once you supply the key",
    "extra_mcps": "Raise the included-MCP quota (metered; each pack adds capacity)",
}


@dataclass(frozen=True)
class Entitlements:
    """What this install is allowed to do. Resolved from a license token;
    the free tier is the default when there's no valid license."""

    tier: Tier
    mcp_quota: int  # max provider groups the user can enable (-1 = unlimited)
    chat_ui: bool  # the gateway/chat interface
    full_app: bool  # the desktop daemon: conductor, all agents, all tools
    agents: tuple[str, ...] | None  # allowed product agents; None = all product agents
    addons: frozenset[str] = field(default_factory=frozenset)
    seats: int = 1
    note: str = ""

    def has_addon(self, name: str) -> bool:
        return name in self.addons

    def allows_agent(self, agent_id: str) -> bool:
        return self.agents is None or agent_id in self.agents

    def effective_mcp_quota(self) -> int:
        """Base quota plus the extra-MCP pack count (license carries the count
        as ``extra_mcps`` once + a ``packs`` field; today one add-on = one pack)."""
        if self.mcp_quota < 0:
            return -1
        return self.mcp_quota + (5 if "extra_mcps" in self.addons else 0)


# The starter agent set for the entry chat tier — odds + stats + the conductor's
# router. Pro unlocks the full roster (modelling, value, arb, fantasy, …).
_PLUS_AGENTS: tuple[str, ...] = (
    "orchestrator", "odds_specialist", "stats_specialist", "concierge",
)

TIERS: dict[Tier, Entitlements] = {
    # No license: enough to evaluate — a couple of MCPs, no chat UI, no daemon.
    "free": Entitlements(
        tier="free", mcp_quota=2, chat_ui=False, full_app=False, agents=(),
        note="Free evaluation — set up 2 MCPs in your own client. Upgrade to chat or run the app.",
    ),
    # Base: the MCP data plane + setup client. Wire them into Claude Desktop / Cursor / etc.
    "base": Entitlements(
        tier="base", mcp_quota=5, chat_ui=False, full_app=False, agents=(),
        note="Data plane access — 5 MCP provider groups in your own MCP client; add more with extra_mcps.",
    ),
    # Plus: the chat interface over the team, a bigger MCP quota.
    "plus": Entitlements(
        tier="plus", mcp_quota=12, chat_ui=True, full_app=False, agents=_PLUS_AGENTS,
        note="Chat interface + 12 MCP groups + the core agents.",
    ),
    # Pro: the full desktop app — everything, plus paid add-ons per license.
    "pro": Entitlements(
        tier="pro", mcp_quota=-1, chat_ui=True, full_app=True, agents=None,
        note="The full app: every agent, every tool, the conductor, unlimited MCPs. Add-ons sold separately.",
    ),
}


def entitlements_for_tier(tier: Tier, addons: frozenset[str] | None = None, seats: int = 1) -> Entitlements:
    base = TIERS[tier]
    # only ever honour add-ons in the catalogue — a mis-issued or unknown add-on
    # never silently grants a capability (the signature already prevents injection;
    # this guards against our own issuance typos and renamed add-ons).
    known = frozenset(a for a in (addons or frozenset()) if a in ADDONS)
    return replace(base, addons=known, seats=seats)


# A source checkout / server deployment with NO baked-in public key is the
# unlicensed-but-trusted case: full access, enforcement is a no-op. Gating only
# activates in a real product BUILD, where the build bakes the pubkey in. This
# keeps `agents …` from source uncrippled and makes free-tier the explicit
# state of a *product* install without a valid license.
_DEV_UNLIMITED = replace(TIERS["pro"], note="unlicensed build — enforcement inactive")


# The whole platform is FREE and open source (2026-07): everyone gets the
# unrestricted entitlements, licence or not. The tier/enforcement machinery is
# kept (dormant) so the seams stay testable and a future hosted/premium offering
# could re-activate it — but no product build gates anything on payment. The
# OPERATOR gate is unrelated to this and still requires a signed operator claim
# (scheduler.is_operator) — that protects the owner's ops deployment, not revenue.
_EVERYONE = replace(TIERS["pro"], note="free & open source — everything included")


def current_entitlements() -> Entitlements:
    """Everyone runs unrestricted — the platform is free (see note above)."""
    return _EVERYONE
