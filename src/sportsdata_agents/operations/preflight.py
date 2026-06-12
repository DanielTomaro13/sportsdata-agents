"""Configuration preflight — one screen of the whole backend config + what's missing.

The backend config is a sprawl of env vars, the OS keychain, and two YAML files;
there's no single place to *see* it. This inventories every setting the operator
cares about and reports each as ok / warn / missing, grouped so you can tell at a
glance whether the install is ready for what you're trying to do (run locally,
take payments, ship updates). All checks are cheap and offline unless ``verify``
is set (which makes one live model call).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["ok", "warn", "missing", "info"]


@dataclass(frozen=True)
class Check:
    group: str
    label: str
    status: Status
    detail: str


def _env(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def run_preflight(*, verify: bool = False) -> list[Check]:
    """Inventory + validate the operator's configuration."""
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.operations.scheduler import is_operator

    settings = get_settings()
    out: list[Check] = []
    add = lambda *a: out.append(Check(*a))  # noqa: E731 - terse local

    # ── Core: what every install needs to actually run ──
    from sportsdata_agents.app.wizard import configured_provider

    provider = configured_provider()
    if provider is None:
        add("Core", "Model provider", "missing", "no model key — run `agents setup`")
    elif verify:
        import asyncio

        from sportsdata_agents.app.wizard import verify_key
        from sportsdata_agents.secrets import get_keychain_secret

        key = os.environ.get(provider.key_env) or get_keychain_secret(provider.key_env) or ""
        ok, why = asyncio.run(verify_key(provider, key))
        add("Core", "Model provider", "ok" if ok else "warn",
            f"{provider.label} — {'verified' if ok else why}")
    else:
        add("Core", "Model provider", "ok", f"{provider.label} (key set)")

    db = settings.database_url
    add("Core", "Warehouse", "ok", "Postgres" if db.startswith("postgresql") else "SQLite (local)")

    mcp_bin = Path(settings.mcp_command[0]) if settings.mcp_command else None
    if mcp_bin and (mcp_bin.exists() or mcp_bin.name == "sportsdata-mcp"):
        add("Core", "Data plane (sportsdata-mcp)", "ok", str(mcp_bin))
    else:
        add("Core", "Data plane (sportsdata-mcp)", "warn", f"binary not found: {mcp_bin}")

    add("Core", "Operator mode", "ok" if is_operator() else "info",
        "ON — platform-maintenance jobs run here" if is_operator()
        else "off — set SPORTSDATA_OPERATOR=1 on YOUR deployment")

    # ── Security: the daemon hardening ──
    add("Security", "Gateway token", "ok" if _env("SPORTSDATA_GATEWAY_TOKEN") else "info",
        "set — mutations require it" if _env("SPORTSDATA_GATEWAY_TOKEN")
        else "off — Host-guard still blocks DNS-rebinding (token is extra)")

    # ── Licensing ──
    pub = os.environ.get("SPORTSDATA_LICENSE_PUBKEY", "")
    add("Licensing", "Licence public key (baked)", "ok" if pub else "info",
        "set — this build ENFORCES tiers" if pub else "unset — unrestricted (dev/source build)")
    add("Licensing", "Licence refresh URL", "ok" if _env("SPORTSDATA_LICENSE_REFRESH_URL") else "info",
        "set" if _env("SPORTSDATA_LICENSE_REFRESH_URL") else "unset — subscriptions can't auto-refresh")

    # ── Commercial (operator, to take payments) ──
    add("Commercial", "Signing private key", "ok" if _env("SPORTSDATA_LICENSE_PRIVKEY") else "missing",
        "set" if _env("SPORTSDATA_LICENSE_PRIVKEY") else "needed to issue licences (`license.py keygen`)")
    add("Commercial", "Product map", "ok" if _env("SPORTSDATA_BILLING_PRODUCTS") else "missing",
        "set" if _env("SPORTSDATA_BILLING_PRODUCTS") else "SPORTSDATA_BILLING_PRODUCTS not set")
    webhook = _env("PADDLE_WEBHOOK_SECRET") or _env("LEMONSQUEEZY_WEBHOOK_SECRET")
    add("Commercial", "Webhook secret", "ok" if webhook else "missing",
        "set" if webhook else "no PADDLE_/LEMONSQUEEZY_WEBHOOK_SECRET")
    smtp = _env("SMTP_HOST")
    add("Commercial", "Email delivery (SMTP)", "ok" if smtp else "warn",
        "set" if smtp else "unset — keys land in the audit log, not the buyer's inbox")

    # ── Updates & alerts ──
    add("Updates", "Data-feed public key", "ok" if _env("SPORTSDATA_DATA_PUBKEY") else "info",
        "set — OTA bundles verified" if _env("SPORTSDATA_DATA_PUBKEY") else "unset — OTA applies unverified (dev)")
    channel = _env("OPS_SLACK_CHANNEL") or _env("OPS_DISCORD_WEBHOOK") or _env("DISCORD_WEBHOOK_URL")
    add("Updates", "Alert channel", "ok" if channel else "info",
        "set — ops reports/alerts push" if channel else "unset — alerts log only")

    return out


def summarise(checks: list[Check]) -> dict[str, int]:
    out = {"ok": 0, "warn": 0, "missing": 0, "info": 0}
    for c in checks:
        out[c.status] += 1
    return out
