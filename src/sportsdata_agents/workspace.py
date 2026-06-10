"""Per-workspace (tenant) configuration.

A *workspace* is the isolated unit of data, secrets, config, and budget (§12). One local
workspace today; the same object scales to many tenants for SaaS. It is the **entitlement
set** the gateway checks before enabling an MCP group / agent / module or starting a run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from .config import Settings, get_settings
from .secrets import resolve_secret

ProvisioningMode = Literal["byo", "managed"]


class Budgets(BaseModel):
    """Cost / loop ceilings, enforced by the gateway. Clamped to the LLM-provisioning mode (§8.1)."""

    per_run_usd: float = 0.50
    monthly_usd: float = 100.0
    max_tool_calls: int = 25
    max_steps: int = 40
    max_tokens: int = 120_000
    timeout_seconds: int = 300


class Workspace(BaseModel):
    """A tenant workspace: what it can use, how it pays for LLMs, and its budgets."""

    tenant_id: str = "local"
    workspace_id: str = "local"

    # Entitlements (catalogue selections). Empty mcp_groups = unrestricted (local dev);
    # in SaaS this is the ceiling each agent scopes within (least privilege, §13).
    enabled_modules: list[str] = Field(default_factory=lambda: ["analytics"])
    mcp_groups: list[str] = Field(default_factory=list)

    # LLM provisioning (§8.1 / D24) + budgets.
    provisioning: ProvisioningMode = "byo"
    budgets: Budgets = Field(default_factory=Budgets)

    # tier -> concrete model overrides (resolved by the model policy at M0.5).
    model_tiers: dict[str, str] = Field(default_factory=dict)

    # Per-workspace secret fallback (env is always preferred).
    # repr=False so secret values never appear in logs/reprs of Workspace (§13).
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)

    def resolve_secret(self, name: str, settings: Settings | None = None) -> SecretStr:
        """Resolve a secret for this workspace: env > workspace secrets > settings secrets."""
        settings = settings or get_settings()
        extra: Mapping[str, str] = {**settings.secrets, **self.secrets}
        return resolve_secret(name, extra)


def default_workspace(settings: Settings | None = None) -> Workspace:
    """The single local workspace, seeded from process settings."""
    settings = settings or get_settings()
    return Workspace(tenant_id=settings.default_tenant, workspace_id=settings.default_workspace)
