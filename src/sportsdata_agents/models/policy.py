"""Model policy (§8): resolve a *tier* to concrete models, and a *task type* to a tier.

The policy is data (``policy.yaml``), not code — vendor-neutral agent specs name a tier
(``fast | balanced | strong``); the policy maps it to a primary model + fallback, with
per-workspace primary overrides (``Workspace.model_tiers``).
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

import yaml
from pydantic import BaseModel, Field

from sportsdata_agents.workspace import Workspace

TIERS = ("fast", "balanced", "strong")


class TierModels(BaseModel):
    default: str
    fallback: str | None = None


class ModelPolicy(BaseModel):
    tiers: dict[str, TierModels]
    routing: dict[str, str] = Field(default_factory=dict)

    def tier_for_task(self, task_type: str) -> str:
        """Map a task type to a tier; unknown tasks get the routing default."""
        return self.routing.get(task_type, self.routing.get("default", "balanced"))

    def models_for_tier(self, tier: str, workspace: Workspace | None = None) -> tuple[str, str | None]:
        """(primary, fallback) for a tier.

        A workspace override replaces the primary AND suppresses the policy fallback:
        the user pinned a provider deliberately, and falling back to a different
        vendor they never configured both violates that pin and masks the primary's
        real error behind that vendor's missing-credentials noise.
        """
        if tier not in self.tiers:
            raise KeyError(f"unknown model tier {tier!r}; expected one of {sorted(self.tiers)}")
        models = self.tiers[tier]
        if workspace is not None and tier in workspace.model_tiers:
            return workspace.model_tiers[tier], None
        return models.default, models.fallback


@lru_cache
def load_policy() -> ModelPolicy:
    """The packaged default policy (a deployment can swap the file)."""
    text = (resources.files("sportsdata_agents.models") / "policy.yaml").read_text(encoding="utf-8")
    return ModelPolicy.model_validate(yaml.safe_load(text))
