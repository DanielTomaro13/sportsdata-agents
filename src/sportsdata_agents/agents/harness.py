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

import contextvars
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from sportsdata_agents.observability.recorder import RunRecorder

from sportsdata_agents.agents.outputs import (
    FINAL_ANSWER_TOOL,
    get_output_type,
    parse_output,
    schema_instructions,
)
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
# Thrash detection: a cycle of up to this period, repeated this many times, stops the
# run. Period 1 = the same call back-to-back; period 2/3 catches a,b,a,b oscillation.
# Safe because tools are reads: an identical repeated cycle cannot produce new info.
NO_PROGRESS_MAX_PERIOD = 3
NO_PROGRESS_REPEATS = 3


def is_thrashing(
    signatures: list[str],
    *,
    max_period: int = NO_PROGRESS_MAX_PERIOD,
    repeats: int = NO_PROGRESS_REPEATS,
) -> bool:
    """True when the tail of ``signatures`` is one cycle (period ≤ max_period) repeated
    ``repeats`` times — e.g. a,a,a or a,b,a,b,a,b."""
    for period in range(1, max_period + 1):
        n = period * repeats
        if len(signatures) < n:
            continue
        window = signatures[-n:]
        block = window[:period]
        if all(window[i] == block[i % period] for i in range(n)):
            return True
    return False
# How many times a failed verification is fed back for another attempt.
VERIFY_RETRIES = 1
# How many times a schema-invalid final answer is fed back for reformatting.
PARSE_RETRIES = 1

# The budget of the currently-executing run, visible to tools (async-safe). This is how
# a delegated sub-agent charges the SAME ceiling as its caller — without it, "per-run"
# would mean per-harness, and a team run could spend ceiling x (1 + delegations) (§16.1).
CURRENT_RUN_BUDGET: contextvars.ContextVar[RunBudget | None] = contextvars.ContextVar(
    "current_run_budget", default=None
)
# The id of the currently-executing run. Delegated sub-runs read it as their
# parent_run_id (audit linkage, §16) — same pattern as the shared budget.
CURRENT_RUN_ID: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "current_run_id", default=None
)
# Per-run recorder override (async-safe). A shared warm harness serves concurrent
# requests; mutating ``harness.recorder`` per request would race — callers pass a
# recorder to ``run()`` instead, and delegated sub-runs inherit it via the context.
CURRENT_RUN_RECORDER: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "current_run_recorder", default=None
)
# Workbench B2 — per-conversation settings, contextvar-scoped so they propagate to
# delegated sub-runs exactly like the shared budget/recorder. The channel seam
# (TeamSession.run) sets them for the root run; nothing else writes them.
# CURRENT_CONV_TIER: the chat's forced model — beats the per-agent pin AND the
# caller's per-run pick (the newest, most specific user intent wins).
CURRENT_CONV_TIER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_conv_tier", default=None
)
# CURRENT_MCP_DENY: provider ids this chat may NOT reach. MCP tool names are
# "<provider>_<op>" (provider ids never contain "_"), so the prefix identifies the
# provider. A UX scope, narrow-only — the licence gate and the B1 global off-switch
# remain the hard boundaries underneath.
CURRENT_MCP_DENY: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "current_mcp_deny", default=None
)


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
# (answer, evidence) → (ok, feedback) — evidence is the run's user + tool message
# contents; the grounding post-check (§13.1) validates numeric claims against it.
Verifier = Callable[[str, list[str]], tuple[bool, str]]


@dataclass
class RunResult:
    output: str
    stop_reason: StopReason
    steps: int
    tool_call_count: int
    cost_usd: float
    verified: bool | None = None
    # The validated output_type instance when the spec declares one and parsing succeeded.
    parsed: Any | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Local file paths tools reported (run_python charts etc.) — channels deliver them
    # (the Slack adapter uploads; the CLI prints paths).
    artifacts: list[str] = field(default_factory=list)
    # This run's id (the AgentRun the recorder persisted) — lets a caller (the chat UI)
    # link the reply to its trace at /runs/{run_id}. None when no recorder/id is in play.
    run_id: uuid.UUID | None = None


def _collect_artifacts(payload: str, into: list[str]) -> None:
    """Harvest artifact paths from a tool's JSON payload (run_python's contract:
    a dict with an ``artifacts`` list) so channels can deliver the files."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return
    if isinstance(data, dict) and isinstance(data.get("artifacts"), list):
        into.extend(str(p) for p in data["artifacts"])


def default_compactor(messages: list[dict[str, Any]], keep_last: int = 6) -> list[dict[str, Any]]:
    """Deterministic compaction stub: keep the system message, **the original task**,
    and the most recent turns, replacing the middle with a marker.

    Contract for any compactor: (1) the result must remain protocol-valid — a ``tool``
    message may only ever directly follow its ``assistant``/``tool`` batch, so the kept
    tail must not *start* with orphaned tool results; (2) the FIRST user message (the
    task) must survive — observed live: a compacted run lost its goal and the model
    narrated "the original user query has been lost" while burning budget guessing.
    """
    if len(messages) <= keep_last + 3:
        return messages
    head = messages[:2]  # system + the original task
    tail = messages[-keep_last:]
    # The slice can cut between an assistant(tool_calls) and its tool results — drop
    # orphaned tool messages at the head of the tail (their pairing was compacted away).
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    dropped = len(messages) - len(head) - len(tail)
    return [
        *head,
        {"role": "user", "content": f"[context compacted: {dropped} earlier messages summarised away]"},
        *tail,
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
        recorder: RunRecorder | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.workspace = workspace
        self.tools: dict[str, ToolDef] = {}
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name {t.name!r} in toolset")
            self.tools[t.name] = t
        self.skills = skills
        self.verifier = verifier
        self.compactor = compactor
        self.recorder = recorder
        self._now = now
        self.output_model = get_output_type(spec.output_type) if spec.output_type else None
        if self.output_model is not None and FINAL_ANSWER_TOOL in self.tools:
            raise ValueError(f"toolset must not define {FINAL_ANSWER_TOOL!r} when output_type is set")

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
        # Clamping is correct (§8.1) but must never be SILENT: a spec author reading
        # "timeout: 600" deserves to know the workspace ceiling made it 300.
        clamped = [
            f"{name} {declared}→{effective}"
            for name, declared, effective in (
                ("max_steps", lim.max_steps, self.max_steps),
                ("max_tool_calls", lim.max_tool_calls, self.max_tool_calls),
                ("cost_ceiling_usd", lim.cost_ceiling_usd, self.cost_ceiling_usd),
                ("timeout_seconds", lim.timeout_seconds, self.timeout_seconds),
                ("max_tokens", lim.max_tokens, self.context_token_limit),
            )
            if effective < declared
        ]
        if clamped:
            logger.info("%s: workspace budget clamps spec limits: %s", spec.id, "; ".join(clamped))

    # ── the loop ───────────────────────────────────────────────────────────

    async def run(
        self,
        user_input: str,
        *,
        budget: RunBudget | None = None,
        recorder: Any | None = None,
        tier: str | None = None,
    ) -> RunResult:
        """Run the loop. ``budget`` shares an existing ceiling (a delegated sub-agent
        charges its caller's budget); omitted = a fresh per-run budget. ``recorder``
        overrides the harness recorder for THIS run only (contextvar-scoped, so a
        shared warm harness can serve concurrent requests without races); delegated
        sub-runs in the same context inherit it. ``tier`` overrides the spec's
        model tier for THIS run only (the orchestrator's complexity-based pick) —
        budgets still clamp regardless of the model chosen."""
        system = self.spec.system_prompt
        if self.skills and len(self.skills):
            system = f"{system}\n\n{self.skills.index_text()}"
        if self.output_model is not None:
            system += schema_instructions(self.output_model)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        self._disclose_skills(messages, user_input)

        budget = budget if budget is not None else RunBudget(ceiling_usd=self.cost_ceiling_usd)
        run_id = uuid.uuid4()
        parent_run_id = CURRENT_RUN_ID.get()
        spent_before = budget.spent_usd
        budget_token = CURRENT_RUN_BUDGET.set(budget)
        run_token = CURRENT_RUN_ID.set(run_id)
        recorder_token = CURRENT_RUN_RECORDER.set(recorder) if recorder is not None else None
        started = time.monotonic()
        await self._record_start(run_id, parent_run_id, user_input)
        # Model precedence (resolved once per run, not per model call): the chat's
        # forced tier (B2 — the newest, most specific user intent) > the user's
        # per-agent pin (B3 — "this agent always uses X") > the caller's per-run
        # pick > the spec default. Budgets clamp regardless of the model chosen.
        from sportsdata_agents.agents.model_prefs import override_for

        pinned = override_for(self.spec.id)
        forced = CURRENT_CONV_TIER.get()
        try:
            result = await self._loop(messages, budget, tier=forced or pinned or tier)
        except BaseException as e:
            # A crashed run must not strand a "running" row + leak its usage buffer.
            await self._record_crash(
                run_id,
                cost_usd=budget.spent_usd - spent_before,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(e).__name__}: {e}",
            )
            raise
        finally:
            CURRENT_RUN_BUDGET.reset(budget_token)
            CURRENT_RUN_ID.reset(run_token)
            if recorder_token is not None:
                CURRENT_RUN_RECORDER.reset(recorder_token)
        await self._record_end(run_id, result, int((time.monotonic() - started) * 1000), recorder)
        return result

    async def _loop(self, messages: list[dict[str, Any]], budget: RunBudget,
                    tier: str | None = None) -> RunResult:
        deadline = self._now() + self.timeout_seconds
        # B2 per-conversation provider scope: a denied provider's tools are not
        # OFFERED to the model this run (and _execute_tool refuses them outright,
        # in case a model hallucinates a name it was never shown).
        deny = CURRENT_MCP_DENY.get()
        tool_schemas = [
            t.schema for t in self.tools.values()
            if not (deny and t.name.split("_", 1)[0] in deny)
        ]
        if self.output_model is not None:
            # The typed output rides the function channel (tool-trained models emit
            # structure there far more reliably than as JSON-in-prose).
            tool_schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": FINAL_ANSWER_TOOL,
                        "description": "Submit your FINAL answer in the required structure.",
                        "parameters": self.output_model.model_json_schema(),
                    },
                }
            )
        steps = 0
        tool_call_count = 0
        verify_attempts = 0
        parse_attempts = 0
        recent_signatures: list[str] = []
        artifacts: list[str] = []  # file paths tools reported (delivered by the channel)
        # Under a SHARED budget (delegation), this run's cost is the delta — reporting
        # budget.spent_usd would double-count the caller's prior spend into sub-run
        # records (and corrupt the M0.11 ledger). Root runs start at 0 → team total.
        spent_before = budget.spent_usd

        def result(
            reason: StopReason, output: str, verified: bool | None = None, parsed: Any | None = None
        ) -> RunResult:
            return RunResult(
                output=output,
                stop_reason=reason,
                steps=steps,
                tool_call_count=tool_call_count,
                cost_usd=budget.spent_usd - spent_before,
                verified=verified,
                parsed=parsed,
                messages=messages,
                artifacts=artifacts,
                # run() set CURRENT_RUN_ID to this run's id before calling _loop, so it's the
                # run's id throughout — lets the chat UI link the reply to /runs/{run_id} (B4).
                run_id=CURRENT_RUN_ID.get(),
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
            try:
                reply = await self.provider.complete(
                    messages,
                    tier=tier or self.spec.model_tier,
                    workspace=self.workspace,
                    budget=budget,
                    tools=tool_schemas or None,
                )
            except BudgetExceededError:
                # The cross-run period budget (operator's daily/weekly/monthly cap) is
                # spent — the gateway refused the call. End the run cleanly, same as the
                # per-run ceiling above, rather than surfacing it as a crash.
                return result("budget_exhausted", last_text)
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
                # ── final answer: typed-output parse, then verification (§13.1) ──
                parsed = None
                if self.output_model is not None:
                    parsed, parse_err = parse_output(reply.text, self.output_model)
                    if parsed is None and parse_attempts < PARSE_RETRIES:
                        parse_attempts += 1
                        # Pydantic errors echo the full invalid input — truncate, or a long
                        # junk answer gets pasted straight back into the window (§8.2).
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"[format] Your answer did not match the required JSON schema: "
                                    f"{parse_err[:400]} — respond ONLY with valid JSON matching the schema."
                                ),
                            }
                        )
                        continue
                if self.spec.context.verify and self.verifier is not None:
                    # Evidence = the user's input + tool results. Harness-INJECTED user
                    # messages are excluded: verifier feedback quotes the fabricated
                    # number (which would self-launder it next round), and skill bodies
                    # carry instructional example figures that are not run data.
                    injected = ("[verifier]", "[format]", "[skill loaded:", "[context compacted")
                    evidence = [
                        content
                        for m in messages
                        if m.get("role") in ("user", "tool")
                        and not (content := str(m.get("content") or "")).startswith(injected)
                    ]
                    ok, feedback = self.verifier(reply.text, evidence)
                    if not ok and verify_attempts < VERIFY_RETRIES:
                        verify_attempts += 1
                        messages.append(
                            {"role": "user", "content": f"[verifier] Your answer failed verification: {feedback}"}
                        )
                        continue
                    return result("done", reply.text, verified=ok, parsed=parsed)
                return result("done", reply.text, parsed=parsed)

            # ── final answer via the function channel? ──
            final_tc = (
                next((tc for tc in reply.tool_calls if tc.name == FINAL_ANSWER_TOOL), None)
                if self.output_model is not None
                else None
            )
            if final_tc is not None and self.output_model is not None:
                # Protocol: every tool_call in the batch needs a tool message.
                for tc in reply.tool_calls:
                    if tc.id != final_tc.id:
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": "skipped: final answer submitted"}
                        )
                answer_text = json.dumps(final_tc.arguments)
                try:
                    parsed = self.output_model.model_validate(final_tc.arguments)
                    parse_err = ""
                except Exception as e:
                    parsed, parse_err = None, str(e)[:400]
                if parsed is None and parse_attempts < PARSE_RETRIES:
                    parse_attempts += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_tc.id,
                            "content": f"error: arguments did not match the schema: {parse_err} — call "
                            f"{FINAL_ANSWER_TOOL} again with corrected arguments.",
                        }
                    )
                    continue
                messages.append({"role": "tool", "tool_call_id": final_tc.id, "content": "accepted"})
                if self.spec.context.verify and self.verifier is not None:
                    injected = ("[verifier]", "[format]", "[skill loaded:", "[context compacted")
                    evidence = [
                        content
                        for m in messages
                        if m.get("role") in ("user", "tool")
                        and not (content := str(m.get("content") or "")).startswith(injected)
                    ]
                    ok, feedback = self.verifier(answer_text, evidence)
                    if not ok and verify_attempts < VERIFY_RETRIES:
                        verify_attempts += 1
                        messages.append(
                            {"role": "user", "content": f"[verifier] Your answer failed verification: {feedback}"}
                        )
                        continue
                    return result("done", answer_text, verified=ok, parsed=parsed)
                return result("done", answer_text, parsed=parsed)

            # ── act: execute the requested tools ──
            # Protocol: ALL of a batch's tool messages must directly follow the assistant
            # message — nothing (skill disclosures included) may interleave. Disclosure
            # happens once, after the whole batch.
            batch_payloads: list[str] = []
            for tc in reply.tool_calls:
                if tool_call_count >= self.max_tool_calls:
                    return result("max_tool_calls", last_text)
                # A tool (especially a delegation) can consume real wall-clock — without
                # this, a batch could overshoot the deadline by (batch size x tool timeout).
                if self._now() >= deadline:
                    return result("timeout", last_text)
                tool_call_count += 1

                signature = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                recent_signatures.append(signature)
                if is_thrashing(recent_signatures):
                    return result("no_progress", last_text)

                payload = await self._execute_tool(tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
                batch_payloads.append(payload)
                _collect_artifacts(payload, artifacts)
            self._disclose_skills(messages, "\n".join(batch_payloads))

    # ── helpers ────────────────────────────────────────────────────────────

    def _active_recorder(self, override: Any | None = None) -> Any | None:
        """The recorder for the CURRENT run: explicit override, then the per-run
        contextvar (set by ``run(recorder=...)``, inherited by delegated sub-runs),
        then the harness default."""
        return override or CURRENT_RUN_RECORDER.get() or self.recorder

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool; errors are returned to the model as content, never raised
        (the model should see the failure and adapt — the loop ceilings bound retries)."""
        started = time.monotonic()
        payload, ok = await self._execute_tool_inner(name, arguments)
        latency_ms = int((time.monotonic() - started) * 1000)
        recorder = self._active_recorder()
        if recorder is not None:
            run_id = CURRENT_RUN_ID.get()
            if run_id is not None:
                try:
                    await recorder.on_tool_call(
                        run_id=run_id, tool=name, arguments=arguments, ok=ok, latency_ms=latency_ms
                    )
                except Exception as e:  # recording must never break a run
                    logger.warning("recorder.on_tool_call failed: %s: %s", type(e).__name__, e)
        return payload

    async def _execute_tool_inner(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if is_denied(name):
            return f"error: tool {name!r} is forbidden by the no-money invariant", False
        deny = CURRENT_MCP_DENY.get()  # B2: scoped-out provider — never shown, never run
        if deny and name.split("_", 1)[0] in deny:
            return f"error: tool {name!r} is outside this conversation's data scope", False
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool {name!r}; available: {sorted(self.tools)}", False
        try:
            output = await tool.execute(arguments)
        except Exception as e:
            logger.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
            return f"error: {type(e).__name__}: {e}", False
        if isinstance(output, str):
            return output, True
        try:
            return json.dumps(output), True
        except (TypeError, ValueError):
            return str(output), True

    async def _record_start(self, run_id: uuid.UUID, parent_run_id: uuid.UUID | None, task: str) -> None:
        recorder = self._active_recorder()
        if recorder is None:
            return
        try:
            await recorder.on_run_start(
                run_id=run_id, parent_run_id=parent_run_id, agent=self.spec.id, task=task
            )
        except Exception as e:  # recording must never break a run
            logger.warning("recorder.on_run_start failed: %s: %s", type(e).__name__, e)

    async def _record_end(
        self, run_id: uuid.UUID, result: RunResult, latency_ms: int, recorder: Any | None = None
    ) -> None:
        recorder = self._active_recorder(recorder)  # explicit: the contextvar reset already
        if recorder is None:
            return
        status = "ok" if result.stop_reason == "done" else result.stop_reason
        try:
            await recorder.on_run_end(
                run_id=run_id, agent=self.spec.id, status=status, cost_usd=result.cost_usd,
                latency_ms=latency_ms, transcript=result.messages,  # M4.5: persist the run's trace
            )
        except Exception as e:
            logger.warning("recorder.on_run_end failed: %s: %s", type(e).__name__, e)

    async def _record_crash(self, run_id: uuid.UUID, *, cost_usd: float, latency_ms: int, error: str) -> None:
        recorder = self._active_recorder()
        if recorder is None:
            return
        try:
            await recorder.on_run_end(
                run_id=run_id,
                agent=self.spec.id,
                status="error",
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                error=error[:500],
            )
        except Exception as e:
            logger.warning("recorder.on_run_end (crash path) failed: %s: %s", type(e).__name__, e)

    def _disclose_skills(self, messages: list[dict[str, Any]], text: str) -> None:
        """Progressive disclosure: append a skill's body the first time it becomes relevant."""
        if not self.skills:
            return
        for skill in self.skills.newly_triggered(text):
            messages.append({"role": "user", "content": f"[skill loaded: {skill.name}]\n{skill.body}"})
            logger.info("skill disclosed: %s", skill.name)
