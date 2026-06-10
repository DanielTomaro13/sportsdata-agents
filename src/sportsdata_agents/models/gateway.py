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
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm

from sportsdata_agents.models.policy import ModelPolicy, load_policy
from sportsdata_agents.workspace import Workspace

logger = logging.getLogger(__name__)


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
    # False when litellm had no pricing for the model (cost metered as 0). BYO: fine —
    # the customer pays the vendor directly. Managed: this MUST be acted on (a $0-metered
    # model is free usage on our keys) — managed mode should allow-list priced models.
    cost_known: bool = True
    fallback_used: bool = False
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    """The gateway's typed result."""

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    tool_calls: tuple[ToolCallRequest, ...] = ()

    @property
    def assistant_message(self) -> dict[str, Any]:
        """The reply as an OpenAI-format assistant message (for transcript append)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return msg


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
        """One completion at a tier: budget-checked, fallback on primary failure, metered.

        Budget semantics: checked **before** the call, charged **after** — so a single
        expensive call can overshoot the ceiling and only the *next* call is refused.
        The hard per-call bound is ``max_tokens`` (the harness sets it from the agent
        spec's limits at M0.7); this is a spend tripwire, not a per-call cap.
        """
        if budget is not None:
            budget.check()

        # A wedged provider must not hang the run: default the call timeout from the
        # workspace budget (callers can still override via kwargs).
        kwargs.setdefault("timeout", workspace.budgets.timeout_seconds)

        primary, fallback = self.policy.models_for_tier(tier, workspace)
        fallback_used = False
        started = time.monotonic()
        try:
            response = await litellm.acompletion(model=primary, messages=list(messages), **kwargs)
            model_used = primary
        except Exception as primary_err:
            if fallback is None:
                raise
            # Surface persistent primary failures to ops (cost-watchdog / eval) instead
            # of silently absorbing them. A double failure chains primary_err as context.
            logger.warning(
                "model fallback: %s failed (%s: %s); retrying on %s",
                primary,
                type(primary_err).__name__,
                primary_err,
                fallback,
            )
            response = await litellm.acompletion(model=fallback, messages=list(messages), **kwargs)
            model_used = fallback
            fallback_used = True
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        cost_usd, cost_known = _cost_of(response)

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
                    cost_known=cost_known,
                    fallback_used=fallback_used,
                )
            )

        text = _text_of(response)
        return ModelReply(
            text=text,
            model=model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            tool_calls=_tool_calls_of(response),
        )


def _text_of(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return content or ""


def _tool_calls_of(response: Any) -> tuple[ToolCallRequest, ...]:
    """Parse OpenAI-format tool calls defensively (litellm normalizes providers to this)."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ()
    out: list[ToolCallRequest] = []
    for i, tc in enumerate(getattr(getattr(choices[0], "message", None), "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        raw_args = getattr(fn, "arguments", "") or "{}"
        try:
            arguments = json.loads(raw_args)
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
        except (json.JSONDecodeError, ValueError):
            arguments = {"_raw": raw_args}
        out.append(
            ToolCallRequest(
                id=str(getattr(tc, "id", "") or f"call_{i}"),
                name=str(getattr(fn, "name", "") or ""),
                arguments=arguments,
            )
        )
    return tuple(out)


def _cost_of(response: Any) -> tuple[float, bool]:
    """(cost, known): litellm's pricing when it knows the model; (0, False) when it doesn't.

    Never crashes a run — but the ``False`` flag must surface on the UsageEvent so managed
    mode can refuse/alert on unpriced models instead of metering them as free (§8.1).
    """
    try:
        return float(litellm.completion_cost(completion_response=response) or 0.0), True
    except Exception:
        return 0.0, False
