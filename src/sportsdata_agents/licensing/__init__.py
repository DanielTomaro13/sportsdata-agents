"""Licensing & entitlements (P4): what a paid install is allowed to do.

The product ships as a downloadable app gated by a paid subscription. A
**license token** is a tiny Ed25519-signed JSON blob — the public key is baked
into the binary, the private key issues licenses — so the app verifies a
license OFFLINE, with no phone-home, and a tampered or expired token simply
fails open to the free tier.

Three tiers (see PRICING.md), each an :class:`Entitlements`:

- **base** — the data plane (MCPs) + a setup client to wire them into the
  user's own MCP client; a quota of provider groups, pay for more.
- **plus** — adds the chat interface (the team over a gateway); a larger MCP
  quota.
- **pro** — the full desktop app: every agent, every tool, the conductor,
  with paid ADD-ONS (Slack, Discord, premium data) toggled per license.

Entitlements are enforced at the seams that already exist — MCP group
enablement, the team roster, the channel adapters — never in prompts.
"""

from .entitlements import (
    ADDONS,
    TIERS,
    Entitlements,
    current_entitlements,
    entitlements_for_tier,
)
from .license import (
    LicenseError,
    issue_license,
    load_license,
    verify_license,
)

__all__ = [
    "ADDONS",
    "TIERS",
    "Entitlements",
    "LicenseError",
    "current_entitlements",
    "entitlements_for_tier",
    "issue_license",
    "load_license",
    "verify_license",
]
