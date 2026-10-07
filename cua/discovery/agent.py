"""Discovery agent: LLM-driven observe → decide → act loop. The ONLY place an LLM decides.

Loop (bounded by limits.discovery_max_steps / discovery_timeout_s):
    obs    = surface.observe()                       (redacted: the model never sees data values)
    reply  = messages.create(system, TOOLS, history) (one tool call per turn, append-only history)
    done       → trace.status = "completed"
    ask_human  → handoff (same session)
    extract    → surface.read() → raw value kept in memory only; the model is told "value withheld"
    otherwise  → resolve ref → verify locator candidates → surface.act() (policy-gated; NeedsHuman →
                 handoff) → settle → observe → recorder.add(TraceAction)
    stuck.record(...) → reason → handoff
A handoff that is not resolved ends the run as "escalated". A refusal, or an API error after the SDK's
bounded retries, ends it as "failed". Until Phase 5 attaches an operator, every handoff times out at once.

API usage (checked against the Claude API docs, 2026-10-06):
  - tool_choice auto + disable_parallel_tool_use: forced `any`/`tool` is rejected on current models,
    so the prompt asks for exactly one tool call and a turn without one is answered with a reminder.
  - strict tools, top-level cache_control (tools + system + growing history), explicit effort.
  - thinking is left at the model default (adaptive); assistant content goes back unchanged.
  - fallbacks="default" (beta server-side-fallback-2026-07-01) only for models the refusal docs cover.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import anthropic
from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError

from cua.compiler.locators import candidates
from cua.config import EVIDENCE_DIR, Policy, Tenant, load_policy, load_tenant
from cua.discovery.prompts import SYSTEM_PROMPT, user_turn
from cua.discovery.recorder import TraceRecorder
from cua.discovery.stuck import StuckDetector
from cua.discovery.tools import TOOLS
from cua.evidence.logger import RunLogger
from cua.handoff.controller import HandoffAborted, SessionControl
from cua.handoff.models import InterventionRequest
from cua.safety.policy import NeedsHuman, PolicyGate, PolicyViolation
from cua.safety.redact import redact_digit_runs
from cua.schema.artifact import Action, Click, Fill, Press, RiskClass, SelectOption
from cua.schema.trace import DiscoveryTrace, ElementSnapshot, TraceAction
from cua.session.provider import LoginFailed, MissingCredentials, SessionProvider
from cua.surface.base import ActionFailed, Observation, Resolved, TargetNotFound
from cua.surface.browser import open_session, start_browser
from cua.surface.playwright_web import PlaywrightWebSurface

DEFAULT_MODEL = "claude-opus-5-5"
EFFORT = "medium"  # explicit: Opus 5.5's default, and changing it mid-run would invalidate the cache
MAX_TOKENS = 16_000  # thinking counts toward it; non-streaming requests stay under SDK timeouts
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models the refusals-and-fallback docs cover (classifier refusals + server-side fallbacks).
FALLBACK_MODELS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
SCRATCH_RUNS = EVIDENCE_DIR / "_scratch"
FAULT_COOKIE = "mb_fault"  # mock bank demo harness only (same as replay)

_OUTPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SUBMIT_KEYS = {"enter", "numpadenter", "return"}
_ACT_TOOLS = {"click", "fill", "select", "press", "dismiss"}

Status = Literal["completed", "escalated", "failed"]


class MessagesAPI(Protocol):
    """The slice of the Anthropic SDK the agent uses (`AsyncAnthropic().beta.messages`). Tests stub it."""

    @property
    def create(self) -> Callable[..., Awaitable[Any]]: ...


@dataclass
class DiscoveryResult:
    trace: DiscoveryTrace
    reason: str  # why the run ended, log-safe
    evidence_dir: Path
    # Raw extracted values, IN MEMORY ONLY (the compiler's self-test compares against them).
    outputs: dict[str, str] = field(default_factory=dict)


class _Stop(Exception):
    def __init__(self, status: Status, reason: str) -> None:
        super().__init__(reason)
        self.status: Status = status
        self.reason = reason


def default_model() -> str:
    return os.getenv("CUA_MODEL") or DEFAULT_MODEL


def action_risk(tool: str, element: ElementSnapshot | None, gate: PolicyGate) -> RiskClass:
    """Risk of a discovery action, decided BEFORE acting. Over-classifying is the safe direction."""
    if tool in ("fill", "select"):
        return RiskClass.reversible
    if tool in ("click", "dismiss") and element is not None:
        label = element.accessible_name or element.text
        if gate.is_irreversible_name(label):
            return RiskClass.irreversible
    return RiskClass.read


def _new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(2)}"


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


async def run_discovery(
    goal: str,
    tenant_id: str,
    params: dict[str, str] | None = None,
    *,
    headless: bool = False,
    fault: str | None = None,
) -> DiscoveryResult:
    """CLI entry: load config, then discover() with the real Anthropic client."""
    return await discover(
        goal,
        load_tenant(tenant_id),
        load_policy(),
        params or {},
        messages_api=anthropic.AsyncAnthropic().beta.messages,
        model=default_model(),
        headed=not headless,
        fault=fault,
    )


async def discover(
    goal: str,
    tenant: Tenant,
    policy: Policy,
    params: dict[str, str],
    *,
    messages_api: MessagesAPI,
    model: str = DEFAULT_MODEL,
    headed: bool = False,
    fault: str | None = None,
    browser: Browser | None = None,
    handoff_timeout_s: float = 0,
    evidence_root: Path = SCRATCH_RUNS,
) -> DiscoveryResult:
    """One discovery run against the live app. Always returns a result; never raises for app/LLM trouble.

    `handoff_timeout_s` is 0 until Phase 5 attaches an operator: an escalation ends the run at once.
    """
    run_id = _new_run_id()
    logger = RunLogger(evidence_root, run_id, "discovery")
    trace = DiscoveryTrace(
        run_id=run_id,
        goal=goal,
        goal_values=dict(params),
        tenant_id=tenant.tenant_id,
        model=model,
        started_at=datetime.now(UTC),
    )
    recorder = TraceRecorder(trace)
    outputs: dict[str, str] = {}
    # The goal names the input values (e.g. a member number): never logged as-is.
    logger.event(
        "run_started",
        goal=redact_digit_runs(goal),
        params=sorted(params),
        tenant=tenant.tenant_id,
        model=model,
    )

    def finish(status: Status, reason: str) -> DiscoveryResult:
        trace.status = status
        logger.event("run_finished", status=status, reason=reason, actions=len(trace.actions))
        return DiscoveryResult(trace=trace, reason=reason, evidence_dir=logger.dir, outputs=outputs)

    gate = PolicyGate(policy)
    try:
        async with AsyncExitStack() as stack:
            if browser is None:
                browser = await stack.enter_async_context(start_browser(headed=headed))
            session = await stack.enter_async_context(open_session(browser, policy, gate, logger))
            if fault:
                await session.context.add_cookies(
                    [{"name": FAULT_COOKIE, "value": fault, "url": tenant.base_url}]
                )
                logger.event("fault_injected", fault=fault)
            try:
                await SessionProvider(tenant).login(session)
            except (LoginFailed, MissingCredentials) as e:
                return finish("failed", f"login failed: {e}")
            logger.event("login_ok")
            control = SessionControl(run_id)
            surface = PlaywrightWebSurface(
                session.page,
                policy,
                gate,
                logger,
                mode="discovery",
                mask_selectors=tenant.mask_selectors,
                control=control,
            )
            agent = _Agent(
                goal, params, policy, gate, logger, surface, control, recorder, outputs,
                messages_api=messages_api, model=model, handoff_timeout_s=handoff_timeout_s,
            )  # fmt: skip
            try:
                async with asyncio.timeout(policy.limits.discovery_timeout_s):
                    reason = await agent.run()
                return finish("completed", reason)
            except TimeoutError:
                try:
                    await agent.handoff("discovery time budget exhausted")
                except _Stop as stop:
                    return finish(stop.status, stop.reason)
                return finish("escalated", "discovery time budget exhausted")
            except _Stop as stop:
                return finish(stop.status, stop.reason)
    except Exception as e:  # browser failed to start / context failed to close / internal error
        return finish("failed", f"internal error: {type(e).__name__}")


# --------------------------------------------------------------------------- #
# One live run
# --------------------------------------------------------------------------- #


@dataclass
class _Outcome:
    """What one tool call produced: the tool_result for the model, and the page to show next."""

    message: str
    ok: bool = True
    done: bool = False
    observation: Observation | None = None  # None: the page did not change (no new observation)


class _Agent:
    def __init__(
        self,
        goal: str,
        params: dict[str, str],
        policy: Policy,
        gate: PolicyGate,
        logger: RunLogger,
        surface: PlaywrightWebSurface,
        control: SessionControl,
        recorder: TraceRecorder,
        outputs: dict[str, str],
        *,
        messages_api: MessagesAPI,
        model: str,
        handoff_timeout_s: float,
    ) -> None:
        self.goal = goal
        self.param_values = set(params.values())  # NEVER logged
        self.policy = policy
        self.gate = gate
        self.logger = logger
        self.surface = surface
        self.control = control
        self.recorder = recorder
        self.outputs = outputs
        self.api = messages_api
        self.model = model
        self.handoff_timeout_s = handoff_timeout_s
        self.max_steps = policy.limits.discovery_max_steps
        self.stuck = StuckDetector(
            max_steps=self.max_steps, repeat_limit=policy.limits.repeated_observation_limit
        )
        self.messages: list[dict[str, Any]] = []
        self.step = 0

    # ---- loop ----------------------------------------------------------------------------------- #

    async def run(self) -> str:
        obs = await self.surface.observe()
        self.messages.append({"role": "user", "content": self._turn(obs)})
        for step in range(1, self.max_steps + 1):
            self.step = step
            response = await self._call()
            self.messages.append({"role": "assistant", "content": response.content})
            call = next((b for b in response.content if b.type == "tool_use"), None)
            if call is None:
                self.logger.event("no_tool_call", stop_reason=response.stop_reason, step=self.step)
                self.messages.append(
                    {"role": "user", "content": "Call exactly one of the provided tools now."}
                )
                await self._check_stuck(obs, ok=False)
                continue

            outcome = await self._dispatch(call.name, dict(call.input), obs, _reasoning(response.content))
            self.logger.event("tool_result", step=self.step, tool=call.name, ok=outcome.ok)
            if outcome.done:
                return outcome.message
            if outcome.observation is not None:
                obs = outcome.observation
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": outcome.message,
            }
            if not outcome.ok:
                result["is_error"] = True
            # Tool results first, then the (new) page: append-only, never edit earlier turns.
            self.messages.append(
                {"role": "user", "content": [result, {"type": "text", "text": self._turn(obs)}]}
            )
            if call.name != "extract":  # reading values never changes the screen; not "stuck"
                await self._check_stuck(obs, ok=outcome.ok)
        await self.handoff("step budget exhausted")
        raise _Stop("escalated", "step budget exhausted")

    def _turn(self, obs: Observation) -> str:
        return user_turn(self.goal, obs.aria_snapshot, self.step + 1, self.max_steps)

    async def _check_stuck(self, obs: Observation, *, ok: bool) -> None:
        reason = self.stuck.record(obs.aria_snapshot, ok)
        if reason:
            await self.handoff(reason)

    # ---- the model -------------------------------------------------------------------------------- #

    async def _call(self) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": self.messages,
            "output_config": {"effort": EFFORT},
            "cache_control": {"type": "ephemeral"},
        }
        if self.model in FALLBACK_MODELS:
            kwargs["fallbacks"] = "default"
            kwargs["betas"] = [FALLBACK_BETA]
        try:
            response = await self.api.create(**kwargs)
        except anthropic.APIError as e:  # the SDK already retried 408/409/429/5xx/connection errors
            raise _Stop("failed", f"model API error: {type(e).__name__}") from None
        usage = getattr(response, "usage", None)
        self.logger.event(
            "llm_turn",
            step=self.step,
            stop_reason=response.stop_reason,
            model=getattr(response, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
        )
        if response.stop_reason == "refusal":  # the whole fallback chain (if any) declined
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise _Stop("failed", f"model refused (category: {category})")
        return response

    # ---- tools ------------------------------------------------------------------------------------ #

    async def _dispatch(
        self, tool: str, args: dict[str, Any], obs: Observation, reasoning: str | None
    ) -> _Outcome:
        if tool == "done":
            return _Outcome(message="done", done=True)
        if tool == "ask_human":
            await self.handoff(
                f"model asked for help: {redact_digit_runs(str(args.get('reason', '')))[:200]}"
            )
            return _Outcome(
                message="A human resolved the situation. Continue.", observation=await self._observe()
            )
        if tool == "extract":
            return await self._extract(args, obs, reasoning)
        if tool in _ACT_TOOLS:
            return await self._act(tool, args, obs, reasoning)
        return _Outcome(message=f"unknown tool '{tool}'", ok=False)

    def _element(self, args: dict[str, Any], obs: Observation) -> tuple[str, ElementSnapshot] | str:
        ref = str(args.get("ref") or "")
        element = obs.refs.get(ref)
        if element is None:
            return f"'{ref}' is not an element ref on the current page; use a ref from the latest snapshot"
        return ref, element

    async def _act(
        self, tool: str, args: dict[str, Any], obs: Observation, reasoning: str | None
    ) -> _Outcome:
        found = self._element(args, obs) if tool != "press" or args.get("ref") else None
        if isinstance(found, str):
            return _Outcome(message=found, ok=False)
        ref, element = found if found else (None, None)

        literal: str | None = None
        action: Action
        if tool == "fill":
            literal = str(args.get("value", ""))
            if literal not in self.param_values:
                # The model may only type what the caller supplied: no invented data, and every typed
                # value can later become an {{inputs.x}} template.
                return _Outcome(message="refused: you may type only values given in the goal", ok=False)
            action = Fill(target=ref or "", value="«typed»")
        elif tool == "select":
            literal = str(args.get("option", ""))
            action = SelectOption(target=ref or "", option=literal)
        elif tool == "press":
            literal = str(args.get("key", ""))
            if literal.lower() in _SUBMIT_KEYS:
                # Enter submits whatever form has focus; a click names its button, so its risk is known.
                return _Outcome(
                    message="refused: click the form's button instead of pressing Enter", ok=False
                )
            action = Press(key=literal, target=ref)
        else:  # click, dismiss
            action = Click(target=ref or "")

        resolved = None
        if ref is not None and element is not None:
            try:
                resolved = await self.surface.resolve_ref(ref)
            except TargetNotFound as e:
                return _Outcome(message=str(e), ok=False)
            element = await self._with_verified_locators(element, resolved)

        risk = action_risk(tool, element, self.gate)
        try:
            await self.surface.act(action, resolved, risk, value=literal if tool == "fill" else None)
        except NeedsHuman as e:
            self._record(tool, element, literal, obs, obs, ok=False, error=str(e), reasoning=reasoning)
            await self.handoff(f"policy: {e}")
            return _Outcome(message="A human handled this step. Continue.", observation=await self._observe())
        except (PolicyViolation, ActionFailed) as e:
            self._record(tool, element, literal, obs, obs, ok=False, error=str(e), reasoning=reasoning)
            return _Outcome(message=f"failed: {e}", ok=False)

        await self.surface.settle(self.policy.limits.step_timeout_ms)
        after = await self._observe()
        self._record(tool, element, literal, obs, after, ok=True, error=None, reasoning=reasoning)
        await self._evidence(tool)
        return _Outcome(message="ok", observation=after)

    async def _extract(self, args: dict[str, Any], obs: Observation, reasoning: str | None) -> _Outcome:
        name = str(args.get("name", ""))
        if not _OUTPUT_NAME_RE.match(name):
            return _Outcome(message="refused: name must be snake_case, e.g. savings_balance", ok=False)
        found = self._element(args, obs)
        if isinstance(found, str):
            return _Outcome(message=found, ok=False)
        ref, element = found
        try:
            resolved = await self.surface.resolve_ref(ref)
            element = await self._with_verified_locators(element, resolved)
            value = await self.surface.read(resolved)  # policy-gated; never logged
        except (TargetNotFound, ActionFailed, PolicyViolation) as e:
            return _Outcome(message=f"failed: {e}", ok=False)
        self.outputs[name] = value
        self.recorder.add(
            TraceAction(
                seq=len(self.recorder.trace.actions) + 1,
                at=datetime.now(UTC),
                actor="llm",
                tool="extract",
                element=element,
                output_name=name,
                page_before=obs.page,
                page_after=obs.page,
                reasoning=reasoning,
            )
        )
        return _Outcome(message=f"recorded {name} (value withheld)")

    async def _with_verified_locators(self, element: ElementSnapshot, resolved: Resolved) -> ElementSnapshot:
        """Keep the candidate locators that match exactly this element on the live page right now."""
        cands = candidates(element)
        checks = await self.surface.verify_locators(resolved, element.frame_path, cands)
        verified = [c for c, ok in zip(cands, checks, strict=True) if ok]
        return element.model_copy(update={"verified_locators": verified})

    def _record(
        self,
        tool: str,
        element: ElementSnapshot | None,
        literal: str | None,
        before: Observation,
        after: Observation,
        *,
        ok: bool,
        error: str | None,
        reasoning: str | None,
    ) -> None:
        self.recorder.add(
            TraceAction(
                seq=len(self.recorder.trace.actions) + 1,
                at=datetime.now(UTC),
                actor="llm",
                tool=tool,  # type: ignore[arg-type]  # one of _ACT_TOOLS, all valid TraceAction tools
                element=element,
                value=literal,  # raw in memory; the recorder redacts on save
                page_before=before.page,
                page_after=after.page,
                ok=ok,
                error=error,
                reasoning=reasoning,
            )
        )

    async def _observe(self) -> Observation:
        return await self.surface.observe()

    async def _evidence(self, tool: str) -> None:
        try:
            await self.surface.snapshot(f"step_{self.step:02d}_{tool}")
        except PlaywrightError as e:
            self.logger.event("snapshot_failed", step=self.step, error=type(e).__name__)

    # ---- handoff ---------------------------------------------------------------------------------- #

    async def handoff(self, reason: str) -> None:
        """Pause and ask a human (same live session). Returns once a human handed back; else _Stop."""
        try:
            shots = await self.surface.snapshot("handoff")
        except PlaywrightError:
            shots = []
        url = self.surface.page.url.split("?", 1)[0]
        request = InterventionRequest(
            intervention_id=uuid4().hex,
            run_id=self.control.run_id,
            mode="discovery",
            goal=redact_digit_runs(self.goal),
            reason=reason,
            current_url=url,
            screenshot=shots[0] if shots else None,
            requested_at=datetime.now(UTC),
        )
        self.logger.event("handoff_requested", step=self.step, reason=reason)
        try:
            await self.control.request_intervention(request, timeout_s=self.handoff_timeout_s)
        except HandoffAborted as e:
            self.logger.event("handoff_ended", step=self.step, resolution=str(e))
            raise _Stop("escalated", reason) from None
        # TODO(phase-5): record the human's actions into the trace (actor="human") and into the model's
        # history, so the compiler can mark them in provenance.human_assisted_steps.
        self.control.resumed()
        self.stuck = StuckDetector(
            max_steps=self.max_steps, repeat_limit=self.policy.limits.repeated_observation_limit
        )
        self.logger.event("handoff_resumed", step=self.step)


def _reasoning(content: list[Any]) -> str | None:
    """Short model rationale for evidence: visible text blocks only, digit runs hashed."""
    text = " ".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text").strip()
    return redact_digit_runs(text)[:300] or None
