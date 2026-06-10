"""The agent harness (§8.2): the loop, loop control, context policy, and verification.

One ``Harness`` binds an :class:`AgentSpec` to a model provider + a toolset and runs the
loop *gather → plan → act (tool) → observe → verify → repeat or stop*. Everything the
plan calls "loop control" lives here, as data on the spec clamped by the workspace
(§8.1): max steps, max tool calls, cost ceiling, wall-clock deadline — plus a
no-progress (thrash) detector and a context budget with compaction/reset.

Runtime note (D28 amendment): the binding is our own loop over ``ModelGateway``
(litellm's cross-provider tool-calling) + ``MCPManager`` — not pydantic-ai — because
the gateway already owns tiers/fallback/budgets/metering and §8.2 needs full loop
control. The agent-spec abstraction stays runtime-neutral.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sportsdata_agents.agents.skills import SkillSet
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.mcp.manager import is_denied
from sportsdata_agents.models.gateway import BudgetExceededError, ModelReply, RunBudget
from sportsdata_agents.workspace import Workspace

logger = logging.getLogger(__name__)

StopReason = Literal[
    "done",
    "max_steps",
    "max_tool_calls",
    "budget_exhausted",
    "timeout",
    "no_progress",
    "context_exhausted",
]

# Fraction of the context-token ceiling at which compaction/reset kicks in.
CONTEXT_THRESHOLD = 0.7
# Identical consecutive tool calls before the thrash detector stops the run.
NO_PROGRESS_REPEATS = 3
# How many times a failed verification is fed back for another attempt.
VERIFY_RETRIES = 1


class CompletionProvider(Protocol):
    """What the harness needs from a model layer (ModelGateway satisfies it)."""

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tier: str = "balanced",
        workspace: Workspace,
        budget: RunBudget | None = None,
        **kwargs: Any,
    ) -> ModelReply: ...


@dataclass(frozen=True)
class ToolDef:
    """A runtime-neutral tool: JSON-schema'd, async-executed (MCP, native, or sub-agent)."""

    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any]], Awaitable[Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# messages-in → messages-out; must preserve the system message.
Compactor = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
# answer → (ok, feedback) — the grounding post-check plugs in here (§13.1, M0.13).
Verifier = Callable[[str], tuple[bool, str]]


@dataclass
class RunResult:
    output: str
    stop_reason: StopReason
    steps: int
    tool_call_count: int
    cost_usd: float
    verified: bool | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


def default_compactor(messages: list[dict[str, Any]], keep_last: int = 6) -> list[dict[str, Any]]:
    """Deterministic compaction stub: keep the system message + the most recent turns,
    replacing the middle with a marker. (An LLM-written summary can replace this later.)"""
    if len(messages) <= keep_last + 2:
        return messages
    dropped = len(messages) - 1 - keep_last
    return [
        messages[0],
        {"role": "user", "content": f"[context compacted: {dropped} earlier messages summarised away]"},
        *messages[-keep_last:],
    ]


class Harness:
    """Runs one agent spec against a toolset under full loop control."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        provider: CompletionProvider,
        workspace: Workspace,
        tools: Sequence[ToolDef] = (),
        skills: SkillSet | None = None,
        verifier: Verifier | None = None,
        compactor: Compactor = default_compactor,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.workspace = workspace
        self.tools = {t.name: t for t in tools}
        self.skills = skills
        self.verifier = verifier
        self.compactor = compactor
        self._now = now

        for name in self.tools:
            if is_denied(name):  # defense in depth — the spec validator already refuses these
                raise ValueError(f"toolset contains denied tool {name!r} (§13)")

        # §8.1: spec limits are clamped to the workspace's ceilings.
        b = workspace.budgets
        lim = spec.limits
        self.max_steps = min(lim.max_steps, b.max_steps)
        self.max_tool_calls = min(lim.max_tool_calls, b.max_tool_calls)
        self.cost_ceiling_usd = min(lim.cost_ceiling_usd, b.per_run_usd)
        self.timeout_seconds = min(lim.timeout_seconds, b.timeout_seconds)
        self.context_token_limit = min(lim.max_tokens, b.max_tokens)

    # ── the loop ───────────────────────────────────────────────────────────

    async def run(self, user_input: str) -> RunResult:
        system = self.spec.system_prompt
        if self.skills and len(self.skills):
            system = f"{system}\n\n{self.skills.index_text()}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        self._disclose_skills(messages, user_input)

        budget = RunBudget(ceiling_usd=self.cost_ceiling_usd)
        deadline = self._now() + self.timeout_seconds
        tool_schemas = [t.schema for t in self.tools.values()]
        steps = 0
        tool_call_count = 0
        verify_attempts = 0
        recent_signatures: list[str] = []

        def result(reason: StopReason, output: str, verified: bool | None = None) -> RunResult:
            return RunResult(
                output=output,
                stop_reason=reason,
                steps=steps,
                tool_call_count=tool_call_count,
                cost_usd=budget.spent_usd,
                verified=verified,
                messages=messages,
            )

        last_text = ""
        while True:
            # ── loop control: every ceiling is checked before spending anything ──
            if steps >= self.max_steps:
                return result("max_steps", last_text)
            if self._now() >= deadline:
                return result("timeout", last_text)
            try:
                budget.check()
            except BudgetExceededError:
                return result("budget_exhausted", last_text)

            steps += 1
            reply = await self.provider.complete(
                messages,
                tier=self.spec.model_tier,
                workspace=self.workspace,
                budget=budget,
                tools=tool_schemas or None,
            )
            messages.append(reply.assistant_message)
            last_text = reply.text or last_text

            # ── context budget: compact or hand off when the window fills (§8.2) ──
            if reply.tokens_in >= self.context_token_limit * CONTEXT_THRESHOLD:
                if self.spec.context.long_run == "compact":
                    before = len(messages)
                    messages = self.compactor(messages)
                    logger.info("context compacted: %d -> %d messages", before, len(messages))
                else:  # "reset": stop so the caller can restart from a structured hand-off
                    return result("context_exhausted", last_text)

            if not reply.tool_calls:
                # ── final answer: verify before accepting (§13.1) ──
                if self.spec.context.verify and self.verifier is not None:
                    ok, feedback = self.verifier(reply.text)
                    if not ok and verify_attempts < VERIFY_RETRIES:
                        verify_attempts += 1
                        messages.append(
                            {"role": "user", "content": f"[verifier] Your answer failed verification: {feedback}"}
                        )
                        continue
                    return result("done", reply.text, verified=ok)
                return result("done", reply.text)

            # ── act: execute the requested tools ──
            for tc in reply.tool_calls:
                if tool_call_count >= self.max_tool_calls:
                    return result("max_tool_calls", last_text)
                tool_call_count += 1

                signature = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                recent_signatures.append(signature)
                if len(recent_signatures) >= NO_PROGRESS_REPEATS and (
                    len(set(recent_signatures[-NO_PROGRESS_REPEATS:])) == 1
                ):
                    return result("no_progress", last_text)

                payload = await self._execute_tool(tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
                self._disclose_skills(messages, payload)

    # ── helpers ────────────────────────────────────────────────────────────

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool; errors are returned to the model as content, never raised
        (the model should see the failure and adapt — the loop ceilings bound retries)."""
        if is_denied(name):
            return f"error: tool {name!r} is forbidden by the no-money invariant"
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool {name!r}; available: {sorted(self.tools)}"
        try:
            output = await tool.execute(arguments)
        except Exception as e:
            logger.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
            return f"error: {type(e).__name__}: {e}"
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output)
        except (TypeError, ValueError):
            return str(output)

    def _disclose_skills(self, messages: list[dict[str, Any]], text: str) -> None:
        """Progressive disclosure: append a skill's body the first time it becomes relevant."""
        if not self.skills:
            return
        for skill in self.skills.newly_triggered(text):
            messages.append({"role": "user", "content": f"[skill loaded: {skill.name}]\n{skill.body}"})
            logger.info("skill disclosed: %s", skill.name)
