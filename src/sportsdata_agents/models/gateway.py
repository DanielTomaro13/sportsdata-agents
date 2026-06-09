"""Model gateway (§8/§8.1): one door to every LLM, with tiers, fallback, budgets, and metering.

- **Tier → model** via the policy (vendor-neutral specs), with per-workspace overrides.
- **Fallback** to the tier's secondary model on a primary failure.
- **Budgets**: a ``RunBudget`` enforces the per-run cost ceiling — exhausted budgets are
  refused *before* the call (§16.1). Who sets the ceiling depends on the provisioning
  mode (§8.1): the customer under BYO, the plan under managed — by this point it's just
  a number to enforce.
- **Metering**: every call emits a ``UsageEvent`` to a sink (M0.11 wires it to the
  ``usage_ledger`` table); cost comes from litellm's pricing map when known.

Key routing note: under BYO the provider keys come from the workspace/environment;
managed platform-key routing is a SaaS-phase concern — the seam is the ``api_key``
parameter resolved per provider, currently left to litellm's env-based resolution.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm

from sportsdata_agents.models.policy import ModelPolicy, load_policy
from sportsdata_agents.workspace import Workspace


class BudgetExceededError(RuntimeError):
    """The run's cost ceiling is spent; the gateway refuses further model calls."""

    def __init__(self, spent_usd: float, ceiling_usd: float) -> None:
        super().__init__(
            f"run budget exhausted: spent ${spent_usd:.4f} of ${ceiling_usd:.4f} ceiling; "
            f"refusing further model calls (§16.1)"
        )
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd


@dataclass
class RunBudget:
    """Per-run cost ceiling. Check before every call; charge after."""

    ceiling_usd: float
    spent_usd: float = 0.0

    def check(self) -> None:
        if self.spent_usd >= self.ceiling_usd:
            raise BudgetExceededError(self.spent_usd, self.ceiling_usd)

    def charge(self, cost_usd: float) -> None:
        self.spent_usd += max(cost_usd, 0.0)

    @classmethod
    def for_workspace(cls, workspace: Workspace) -> RunBudget:
        return cls(ceiling_usd=workspace.budgets.per_run_usd)


@dataclass(frozen=True)
class UsageEvent:
    """One metered model call — the row M0.11 writes to `usage_ledger` (§16.1)."""

    kind: str  # "llm"
    model: str
    tier: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    tenant_id: str
    workspace_id: str
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@dataclass(frozen=True)
class ModelReply:
    """The gateway's typed result."""

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


UsageSink = Callable[[UsageEvent], None]


class ModelGateway:
    """Tier-routed completions over litellm with fallback, budget, and metering."""

    def __init__(
        self,
        *,
        policy: ModelPolicy | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        self.policy = policy or load_policy()
        self._sink = usage_sink

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tier: str = "balanced",
        workspace: Workspace,
        budget: RunBudget | None = None,
        **kwargs: Any,
    ) -> ModelReply:
        """One completion at a tier: budget-checked, fallback on primary failure, metered."""
        if budget is not None:
            budget.check()

        primary, fallback = self.policy.models_for_tier(tier, workspace)
        started = time.monotonic()
        try:
            response = await litellm.acompletion(model=primary, messages=list(messages), **kwargs)
            model_used = primary
        except Exception:
            if fallback is None:
                raise
            response = await litellm.acompletion(model=fallback, messages=list(messages), **kwargs)
            model_used = fallback
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        cost_usd = _cost_of(response)

        if budget is not None:
            budget.charge(cost_usd)
        if self._sink is not None:
            self._sink(
                UsageEvent(
                    kind="llm",
                    model=model_used,
                    tier=tier,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    tenant_id=workspace.tenant_id,
                    workspace_id=workspace.workspace_id,
                )
            )

        text = _text_of(response)
        return ModelReply(text=text, model=model_used, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)


def _text_of(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return content or ""


def _cost_of(response: Any) -> float:
    """litellm's pricing map when it knows the model; 0 when it doesn't (never crash a run)."""
    try:
        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        return 0.0
