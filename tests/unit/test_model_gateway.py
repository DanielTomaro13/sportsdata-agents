"""M0.5 — model policy + gateway (litellm fully mocked; no API spend)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from sportsdata_agents.models import gateway as gw
from sportsdata_agents.models.gateway import (
    BudgetExceededError,
    ModelGateway,
    RunBudget,
    UsageEvent,
)
from sportsdata_agents.models.policy import load_policy
from sportsdata_agents.workspace import Budgets, Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


# ── fake litellm surface ─────────────────────────────────────────────────


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 20


@dataclass
class _Msg:
    content: str = "answer"


@dataclass
class _Choice:
    message: _Msg = field(default_factory=_Msg)


@dataclass
class _Resp:
    model: str = ""
    usage: _Usage = field(default_factory=_Usage)
    choices: list[_Choice] = field(default_factory=lambda: [_Choice()])


class _FakeLiteLLM:
    """Records calls + kwargs; optionally fails specific models."""

    def __init__(self, fail_models: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.fail_models = fail_models or set()

    async def acompletion(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> _Resp:
        self.calls.append(model)
        self.kwargs.append(kw)
        if model in self.fail_models:
            raise RuntimeError(f"simulated failure for {model}")
        return _Resp(model=model)

    def completion_cost(self, *, completion_response: Any) -> float:
        return 0.01


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeLiteLLM:
    f = _FakeLiteLLM()
    monkeypatch.setattr(gw, "litellm", f)
    return f


# ── policy ───────────────────────────────────────────────────────────────


def test_policy_loads_and_routes() -> None:
    p = load_policy()
    assert set(p.tiers) == {"fast", "balanced", "strong"}
    # routing is now the DELEGATION complexity map (the orchestrator's pick):
    assert p.tier_for_task("simple") == "fast"
    assert p.tier_for_task("complex") == "strong"
    assert p.tier_for_task("standard") == ""  # the specialist's own tier stands
    assert p.tier_for_task("nonsense") == ""  # unknown = no override, never escalate


def test_policy_workspace_override_pins_provider_and_suppresses_fallback() -> None:
    """A pinned provider must not fall back to a vendor the user never configured —
    it violates the pin and masks the primary's real error behind missing-credential
    noise from the fallback vendor."""
    p = load_policy()
    ws = Workspace(model_tiers={"balanced": "groq/llama"})
    primary, fallback = p.models_for_tier("balanced", ws)
    assert primary == "groq/llama"
    assert fallback is None


def test_policy_unknown_tier_raises() -> None:
    with pytest.raises(KeyError):
        load_policy().models_for_tier("ultra")


# ── gateway ──────────────────────────────────────────────────────────────


async def test_complete_uses_primary_and_meters(fake: _FakeLiteLLM) -> None:
    events: list[UsageEvent] = []
    g = ModelGateway(usage_sink=events.append)
    reply = await g.complete([{"role": "user", "content": "hi"}], tier="balanced", workspace=WS)

    assert reply.text == "answer"
    assert reply.model == fake.calls[0]
    assert reply.tokens_in == 100 and reply.tokens_out == 20
    assert reply.cost_usd == pytest.approx(0.01)
    assert len(events) == 1
    ev = events[0]
    assert (ev.tenant_id, ev.workspace_id, ev.kind, ev.tier) == ("t", "w", "llm", "balanced")


async def test_fallback_on_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = load_policy()
    primary, fallback = policy.models_for_tier("balanced")
    fake = _FakeLiteLLM(fail_models={primary})
    monkeypatch.setattr(gw, "litellm", fake)

    g = ModelGateway()
    reply = await g.complete([{"role": "user", "content": "hi"}], tier="balanced", workspace=WS)
    assert fake.calls == [primary, fallback]
    assert reply.model == fallback


async def test_both_models_failing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = load_policy()
    primary, fallback = policy.models_for_tier("balanced")
    fake = _FakeLiteLLM(fail_models={primary, str(fallback)})
    monkeypatch.setattr(gw, "litellm", fake)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="balanced", workspace=WS)


async def test_exhausted_budget_refused_before_any_call(fake: _FakeLiteLLM) -> None:
    budget = RunBudget(ceiling_usd=0.05, spent_usd=0.05)
    with pytest.raises(BudgetExceededError):
        await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=WS, budget=budget)
    assert fake.calls == []  # refused up front — no model traffic


async def test_budget_accumulates_and_trips(fake: _FakeLiteLLM) -> None:
    g = ModelGateway()
    budget = RunBudget(ceiling_usd=0.025)  # two 1c calls fit, the third must not
    msgs = [{"role": "user", "content": "hi"}]
    await g.complete(msgs, tier="fast", workspace=WS, budget=budget)
    await g.complete(msgs, tier="fast", workspace=WS, budget=budget)
    assert budget.spent_usd == pytest.approx(0.02)
    await g.complete(msgs, tier="fast", workspace=WS, budget=budget)  # 0.02 < 0.025 → allowed
    with pytest.raises(BudgetExceededError):
        await g.complete(msgs, tier="fast", workspace=WS, budget=budget)
    assert len(fake.calls) == 3


def test_run_budget_from_workspace() -> None:
    ws = Workspace(budgets=Budgets(per_run_usd=0.75))
    assert RunBudget.for_workspace(ws).ceiling_usd == 0.75


async def test_cost_failure_never_crashes_and_is_flagged(fake: _FakeLiteLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown pricing meters 0 but flags cost_known=False (managed mode must act on it, §8.1)."""
    monkeypatch.setattr(fake, "completion_cost", lambda **_: (_ for _ in ()).throw(ValueError("no pricing")))
    events: list[UsageEvent] = []
    reply = await ModelGateway(usage_sink=events.append).complete(
        [{"role": "user", "content": "hi"}], tier="fast", workspace=WS
    )
    assert reply.cost_usd == 0.0
    assert events[0].cost_known is False


async def test_call_timeout_tighter_of_default_and_budget(fake: _FakeLiteLLM) -> None:
    """A wedged provider can't hang the run — and one CALL can't eat the run's whole
    deadline either: the per-call timeout is min(120s default, workspace budget)."""
    ws = Workspace(budgets=Budgets(timeout_seconds=42))  # tighter than the 120s default
    await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=ws)
    assert fake.kwargs[0]["timeout"] == 42
    roomy = Workspace(budgets=Budgets(timeout_seconds=600))  # default caps it at 120
    await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=roomy)
    assert fake.kwargs[1]["timeout"] == 120.0
    # caller override wins
    await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=ws, timeout=7)
    assert fake.kwargs[2]["timeout"] == 7


async def test_output_token_cap_defaults_and_overrides(fake: _FakeLiteLLM) -> None:
    """The per-call output cap (§16.1's hard per-call bound) rides every call —
    prepaid providers reserve max_tokens upfront, so an uncapped default can fail
    against a small credit balance."""
    from sportsdata_agents.models.gateway import DEFAULT_MAX_OUTPUT_TOKENS

    await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=WS)
    assert fake.kwargs[0]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=WS, max_tokens=128)
    assert fake.kwargs[1]["max_tokens"] == 128


async def test_rate_limit_patience_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """429s wait out the provider's stated retry-after instead of dying mid-run."""
    import sportsdata_agents.models.gateway as gwmod

    class RateLimitError(Exception):
        pass

    calls = {"n": 0}
    sleeps: list[float] = []

    class FlakyLiteLLM(_FakeLiteLLM):
        async def acompletion(self, *, model, messages, **kw):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitError("rate limit: Please try again in 0.5s")
            return await super().acompletion(model=model, messages=messages, **kw)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(gwmod, "litellm", FlakyLiteLLM())
    monkeypatch.setattr(gwmod.asyncio, "sleep", fake_sleep)
    reply = await ModelGateway().complete([{"role": "user", "content": "hi"}], tier="fast", workspace=WS)
    assert reply.text == "answer"
    assert calls["n"] == 2
    assert sleeps and sleeps[0] == pytest.approx(1.5)  # retry-after 0.5 + 1.0 buffer


async def test_fallback_is_logged_and_flagged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    policy = load_policy()
    primary, fallback = policy.models_for_tier("balanced")
    fake = _FakeLiteLLM(fail_models={primary})
    monkeypatch.setattr(gw, "litellm", fake)

    events: list[UsageEvent] = []
    with caplog.at_level("WARNING", logger="sportsdata_agents.models.gateway"):
        await ModelGateway(usage_sink=events.append).complete(
            [{"role": "user", "content": "hi"}], tier="balanced", workspace=WS
        )
    assert any("model fallback" in r.getMessage() for r in caplog.records)
    assert events[0].fallback_used is True
    assert events[0].model == fallback
