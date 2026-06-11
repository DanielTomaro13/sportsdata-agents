"""Agent-spec models (§7) — the runtime-neutral contract an agent is declared in.

A spec is YAML: ``spec_version`` + an ``agent`` block. Users author these (directly,
or via the agent-builder); the runtime (M0.7) binds them to an executable agent. The
schema is strict (``extra="forbid"``) so a typo'd field fails loudly instead of being
silently ignored, and the no-money invariant is enforced at validation time: a spec
simply cannot name a money-ish tool or capability (§13).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sportsdata_agents.mcp.manager import is_denied
from sportsdata_agents.models.policy import TIERS

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
# Capability tags and MCP groups are dotted lowercase (e.g. `sport.prices`, `mlb.reference`).
DOTTED_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def _validate_names(kind: str, names: list[str], pattern: re.Pattern[str]) -> list[str]:
    """Shape-check a name list and reject duplicates — typos must fail at authoring time."""
    seen: set[str] = set()
    for name in names:
        if not pattern.match(name):
            raise ValueError(f"{kind} {name!r} must match {pattern.pattern}")
        if name in seen:
            raise ValueError(f"duplicate {kind} {name!r}")
        seen.add(name)
    return names


class ToolsSpec(BaseModel):
    """What an agent may call. Prefer capabilities (cross-provider) over raw groups."""

    model_config = ConfigDict(extra="forbid")

    mcp_capabilities: list[str] = Field(default_factory=list)
    mcp_groups: list[str] = Field(default_factory=list)
    native: list[str] = Field(default_factory=list)

    @field_validator("mcp_capabilities")
    @classmethod
    def _caps_shape(cls, v: list[str]) -> list[str]:
        return _validate_names("mcp capability", v, DOTTED_PATTERN)

    @field_validator("mcp_groups")
    @classmethod
    def _groups_shape(cls, v: list[str]) -> list[str]:
        return _validate_names("mcp group", v, DOTTED_PATTERN)

    @field_validator("native")
    @classmethod
    def _native_shape(cls, v: list[str]) -> list[str]:
        return _validate_names("native tool", v, ID_PATTERN)


class ContextPolicy(BaseModel):
    """Harness/context policy (§8.2)."""

    model_config = ConfigDict(extra="forbid")

    retrieval: Literal["jit", "preload"] = "jit"
    long_run: Literal["compact", "reset"] = "compact"
    verify: bool = True


class Limits(BaseModel):
    """Per-run ceilings; clamped to the workspace's provisioning mode at runtime (§8.1)."""

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=25, gt=0)
    max_steps: int = Field(default=40, gt=0)
    max_tokens: int = Field(default=120_000, gt=0)
    timeout_seconds: int = Field(default=300, gt=0)
    cost_ceiling_usd: float = Field(default=0.50, gt=0)


class AgentSpec(BaseModel):
    """One declared agent (the ``agent:`` block of a spec file)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str = ""
    version: str = "0.1.0"
    # §3.1 two-plane split: "product" agents serve tenants through the customer
    # gateway; "ops" agents run under the PLATFORM's identity with platform creds
    # (GitHub/CI) and are reachable only from the operator CLI — never the gateway.
    plane: Literal["product", "ops"] = "product"
    model_tier: str = "balanced"
    system_prompt: str = Field(min_length=1)
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    skills: list[str] = Field(default_factory=list)
    forbidden_capabilities: list[str] = Field(default_factory=list)
    can_delegate_to: list[str] = Field(default_factory=list)
    sandbox: Literal["none", "ephemeral"] = "none"
    secrets: list[str] = Field(default_factory=list)
    output_type: str | None = None
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not ID_PATTERN.match(v):
            raise ValueError(f"agent id {v!r} must match {ID_PATTERN.pattern}")
        return v

    @field_validator("skills")
    @classmethod
    def _skills_shape(cls, v: list[str]) -> list[str]:
        return _validate_names("skill", v, ID_PATTERN)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_PATTERN.match(v):
            raise ValueError(f"version {v!r} must be semver (e.g. 0.1.0) — D27")
        return v

    @field_validator("model_tier")
    @classmethod
    def _tier_or_explicit_model(cls, v: str) -> str:
        # A tier name, or an explicit provider-qualified model ("anthropic/claude-...").
        if v in TIERS or "/" in v or ":" in v:
            return v
        raise ValueError(f"model_tier {v!r} must be one of {TIERS} or an explicit 'provider/model'")

    @model_validator(mode="after")
    def _no_money_anywhere(self) -> AgentSpec:
        """The no-money invariant, enforced at authoring time (§13)."""
        for kind, names in (
            ("native tool", self.tools.native),
            ("mcp capability", self.tools.mcp_capabilities),
            ("skill", self.skills),
        ):
            for name in names:
                if is_denied(name):
                    raise ValueError(f"{kind} {name!r} trips the no-money deny-filter (§13); specs cannot grant it")
        return self

    @model_validator(mode="after")
    def _forbidden_does_not_overlap_allowed(self) -> AgentSpec:
        overlap = set(self.forbidden_capabilities) & set(self.tools.mcp_capabilities)
        if overlap:
            raise ValueError(f"capabilities both allowed and forbidden: {sorted(overlap)}")
        return self


class AgentSpecFile(BaseModel):
    """The on-disk document: ``spec_version`` + one agent."""

    model_config = ConfigDict(extra="forbid")

    spec_version: int = 1
    agent: AgentSpec
