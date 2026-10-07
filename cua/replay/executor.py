"""Deterministic replay engine. No LLM imports allowed in this package.

    load artifact → apply tenant overrides → effective artifact (digest logged)
    pre-flight (review status, confirmation, app version, inputs) + template resolution  → rejected
    open context → SessionProvider.login → app.fingerprint                → failed / APP_VERSION_MISMATCH
    for step in steps:
        resolve target (bounded wait) → act / read                       (policy-gated inside Surface)
        race: conditions in declared order → checkpoint → timeout = UNKNOWN_STATE
        handler: return_outcome | fail | escalate | dismiss | wait_until_clear | reauthenticate
    artifact.success holds → RunResult(success, outputs)

Escalation (operator attached): pause on the SAME session → human takes control, fixes, hands back →
resync: resume after the furthest `expect` checkpoint (from the current step on) that holds; none →
retry the current step if its action never ran, else ask again with the expected state. Bounded
(_MAX_RESYNCS hand-backs per run); never resumes past an irreversible step a human performed.
No operator → the escalation ends the run at once (resolution aborted, "no operator available").

Every terminal path returns a RunResult. Evidence (run.jsonl, masked screenshots, failure DOM,
effective artifact, redacted result.json) is written under <evidence_root>/replay_<run_id>/.
"""

from __future__ import annotations

import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError

from cua import catalog
from cua.config import EVIDENCE_DIR, ROOT, Policy, Tenant, load_policy, load_tenant
from cua.evidence.logger import RunLogger
from cua.handoff.controller import ControlState, HandoffAborted, NotInControl, SessionControl
from cua.handoff.models import InterventionRequest
from cua.handoff.operator import OperatorServer
from cua.handoff.recorder import HumanRecorder
from cua.replay.checks import Matched, Satisfied, evaluate, match_condition, race
from cua.replay.extract import ParseError, parse_value
from cua.replay.overrides import apply_overrides
from cua.replay.preflight import preflight
from cua.replay.resolver import resolve_target
from cua.safety.policy import NeedsHuman, PolicyGate, PolicyViolation
from cua.safety.redact import redact, redact_mapping
from cua.schema.artifact import (
    TEMPLATE_RE,
    CapabilityArtifact,
    Check,
    Click,
    Dismiss,
    Escalate,
    Extract,
    Fail,
    FailureCategory,
    Fill,
    KnownCondition,
    Navigate,
    OutcomeKind,
    Reauthenticate,
    ReturnOutcome,
    RiskClass,
    Step,
    ValueType,
    WaitUntilClear,
)
from cua.schema.result import Failure, Handoff, Recovery, RunResult, RunStatus, Scalar
from cua.session.provider import LoginFailed, MissingCredentials, SessionProvider
from cua.surface.base import ActionFailed, Resolved
from cua.surface.browser import BrowserSession, open_session, start_browser
from cua.surface.playwright_web import PlaywrightWebSurface
from cua.surface.wait import poll_until

RunMode = Literal["supervised", "unattended"]

SCRATCH_RUNS = EVIDENCE_DIR / "_scratch"
FAULT_COOKIE = "mb_fault"  # mock bank demo harness only (see DESIGN.md decisions log)

# Routing principle. ESCALATE when a human at the screen could fix it (dismiss an unknown page,
# sign in, wait out an outage, point at the moved control): UNKNOWN_STATE, TARGET_*,
# RECOVERY_EXHAUSTED, SESSION_EXPIRED after a failed re-auth, TIMEOUT, and conditions whose handler
# is `escalate`. FAIL without escalating when a click can't or mustn't fix it: POLICY_VIOLATION
# (never overridden mid-run), APP_VERSION_MISMATCH (wrong app), CHECKPOINT_FAILED (wrong data was
# read), and conditions whose handler is `fail`. See _escalate() and _fail().
_RETRYABLE = {
    FailureCategory.TIMEOUT,
    FailureCategory.RECOVERY_EXHAUSTED,
    FailureCategory.SESSION_EXPIRED,
}

_PARSE_TYPE = {
    "text": ValueType.string,
    "integer": ValueType.integer,
    "decimal": ValueType.decimal,
    "currency": ValueType.decimal,
    "date": ValueType.date,
}


_MAX_RESYNCS = 3  # hand-backs per run; then the run fails (resolution aborted)
_RESYNC_WAIT_MS = 5_000  # after hand-back: how long a checkpoint may take to appear (page still loading)


class _Resume(Exception):
    """A handoff was resolved: continue the step loop at `index` (== len(steps) → final check)."""

    def __init__(self, index: int) -> None:
        super().__init__(index)
        self.index = index


class _Stop(Exception):
    """Unwinds the run to a terminal status."""

    def __init__(
        self, status: RunStatus, *, outcome_code: str | None = None, failure: Failure | None = None
    ) -> None:
        super().__init__(status.value)
        self.status = status
        self.outcome_code = outcome_code
        self.failure = failure


def _new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(2)}"


def _describe(check: Check | None) -> str:
    if check is None:
        return "the step completes with no known condition on screen"

    def one(p: object) -> str:
        fields = {k: v for k, v in vars(p).items() if k != "kind" and v is not None}
        return f"{getattr(p, 'kind', '?')}({', '.join(str(v) for v in fields.values())})"

    parts = []
    if check.all_of:
        parts.append(" and ".join(one(p) for p in check.all_of))
    if check.any_of:
        parts.append("any of: " + ", ".join(one(p) for p in check.any_of))
    return "; ".join(parts)


def _safe_url(url: str) -> str:
    """Scheme, host and path only: query strings can carry data."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _scalar(value: str | int | Decimal | date) -> Scalar:
    """Money stays exact: decimals become canonical strings, never floats. Dates become ISO strings."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _resolve_templates(
    artifact: CapabilityArtifact, inputs: dict[str, str], tenant: Tenant
) -> dict[str, str] | Failure:
    """Resolve every navigate/fill template up front, so a bad template is refused before the UI."""
    tenant_values = {k: v for k, v in tenant.model_dump().items() if isinstance(v, str)}
    scopes = {"inputs": inputs, "tenant": tenant_values}
    values: dict[str, str] = {}
    for step in artifact.steps:
        a = step.action
        raw = a.url if isinstance(a, Navigate) else a.value if isinstance(a, Fill) else None
        if raw is None:
            continue
        for scope, name in TEMPLATE_RE.findall(raw):
            # {{secrets.x}} has no vault in this build: refuse rather than guess where it lives.
            if scope == "secrets" or (scope == "tenant" and name not in tenant_values):
                return Failure(
                    category=FailureCategory.POLICY_VIOLATION,
                    step_id=step.id,
                    expected="templates resolvable from inputs or tenant config",
                    observed=f"unsupported template '{scope}.{name}'",
                    retryable=False,
                )
        values[step.id] = TEMPLATE_RE.sub(lambda m: scopes[m.group(1)].get(m.group(2), ""), raw)
    return values


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


async def replay(
    capability_id: str,
    tenant_id: str,
    inputs: dict[str, str],
    *,
    version: str | None = None,
    mode: RunMode = "unattended",
    confirmed: bool = False,
    fault: str | None = None,
    handoff_timeout_s: float | None = None,
    operator: OperatorServer | None = None,
    headed: bool = False,
    evidence_root: Path = SCRATCH_RUNS,
) -> RunResult:
    """CLI entry: load config + catalog, then execute()."""
    return await execute(
        catalog.load(capability_id, version),
        load_tenant(tenant_id),
        load_policy(),
        inputs,
        mode=mode,
        confirmed=confirmed,
        fault=fault,
        handoff_timeout_s=handoff_timeout_s,
        operator=operator,
        headed=headed,
        evidence_root=evidence_root,
    )


async def execute(
    artifact: CapabilityArtifact,
    tenant: Tenant,
    policy: Policy,
    inputs: dict[str, str],
    *,
    mode: RunMode = "unattended",
    confirmed: bool = False,
    fault: str | None = None,
    browser: Browser | None = None,
    handoff_timeout_s: float | None = None,
    operator: OperatorServer | None = None,
    headed: bool = False,
    evidence_root: Path = SCRATCH_RUNS,
) -> RunResult:
    """Run one capability. A browser is launched (unless given) only after pre-flight passes.

    With an `operator`, the run registers its live SessionControl there and escalations wait (up to
    the handoff timeout) for a human; without one, an escalation ends the run at once.
    """
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    logger = RunLogger(evidence_root, _new_run_id(), "replay")
    input_sens = {i.name: i.sensitivity for i in artifact.contract.inputs}
    output_sens = {o.name: o.sensitivity for o in artifact.contract.outputs}
    recoveries: list[Recovery] = []
    handoffs: list[Handoff] = []

    def finish(
        status: RunStatus,
        *,
        outcome_code: str | None = None,
        failure: Failure | None = None,
        outputs: dict[str, Scalar] | None = None,
    ) -> RunResult:
        try:
            evidence_dir = str(logger.dir.relative_to(ROOT))
        except ValueError:
            evidence_dir = str(logger.dir)
        result = RunResult(
            run_id=logger.run_id,
            mode="replay",
            capability_id=artifact.id,
            capability_version=artifact.version,
            tenant_id=tenant.tenant_id,
            status=status,
            outcome_code=outcome_code,
            outputs=outputs or {},
            failure=failure,
            recoveries=recoveries,
            handoffs=handoffs,
            started_at=started_at,
            duration_ms=int((time.monotonic() - t0) * 1000),
            evidence_dir=evidence_dir,
        )
        # The caller gets real values; evidence gets them redacted by output sensitivity.
        logger.write_json(
            "result.json", result.model_copy(update={"outputs": redact_mapping(result.outputs, output_sens)})
        )
        logger.event(
            "run_finished",
            status=status.value,
            outcome_code=outcome_code,
            category=failure.category.value if failure else None,
            duration_ms=result.duration_ms,
        )
        return result

    logger.event(
        "run_started",
        capability=artifact.id,
        version=artifact.version,
        tenant=tenant.tenant_id,
        run_mode=mode,
        inputs=redact_mapping(inputs, input_sens),
    )

    # ---- before the UI: everything here is `rejected`, no browser ---------------------------- #
    try:
        effective, digest = apply_overrides(artifact, tenant.overrides.get(artifact.id))
    except ValueError as e:  # includes pydantic ValidationError
        return finish(
            RunStatus.rejected,
            failure=Failure(
                category=FailureCategory.POLICY_VIOLATION,
                expected="tenant overrides that patch only targets/conditions and keep the artifact valid",
                observed=str(e).splitlines()[0],
                retryable=False,
            ),
        )
    logger.event("effective_artifact", digest=digest)
    logger.write_json("artifact.json", effective)

    pre = preflight(effective, inputs, policy, mode=mode, confirmed=confirmed, tenant=tenant)
    if isinstance(pre, Failure):
        logger.event("rejected", category=pre.category.value, expected=pre.expected, observed=pre.observed)
        return finish(RunStatus.rejected, failure=pre)
    values = _resolve_templates(effective, inputs, tenant)
    if isinstance(values, Failure):
        logger.event("rejected", category=values.category.value, observed=values.observed)
        return finish(RunStatus.rejected, failure=values)
    logger.event("preflight_ok")

    # ---- against the live app ------------------------------------------------------------------ #
    gate = PolicyGate(policy)
    run: _Run | None = None
    try:
        async with AsyncExitStack() as stack:
            if browser is None:
                browser = await stack.enter_async_context(start_browser(headed=headed))
            session = await stack.enter_async_context(open_session(browser, policy, gate, logger))
            run = _Run(
                effective,
                tenant,
                policy,
                gate,
                logger,
                session,
                values,
                recoveries=recoveries,
                handoffs=handoffs,
                confirmed=confirmed,
                handoff_timeout_s=(
                    policy.limits.handoff_timeout_s if handoff_timeout_s is None else handoff_timeout_s
                ),
                operator=operator,
            )
            if operator is not None:
                entry = operator.register(logger.run_id, run.control, logger.dir, mode="replay")
                entry.human_actions = run.human_actions  # live, redacted: the operator page lists them
            try:
                outcome_code, outputs = await run.go(fault)
            except _Stop as stop:
                return finish(stop.status, outcome_code=stop.outcome_code, failure=stop.failure)
            except Exception as e:  # never leave a run without a RunResult (invariant 6)
                evidence = await run.snapshot("internal_error", dom=True)
                return finish(RunStatus.failed, failure=_internal(e, evidence))
            finally:
                if operator is not None:
                    operator.unregister(logger.run_id)
            return finish(RunStatus.success, outcome_code=outcome_code, outputs=outputs)
    except Exception as e:  # browser failed to start / context failed to close
        return finish(RunStatus.failed, failure=_internal(e, []))


def _internal(e: Exception, evidence: list[str]) -> Failure:
    # Type name only: exception text may carry page content or typed values.
    return Failure(
        category=FailureCategory.UNKNOWN_STATE,
        expected="the run to reach a known terminal state",
        observed=f"internal error: {type(e).__name__}",
        retryable=False,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# One live run
# --------------------------------------------------------------------------- #


class _Run:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        tenant: Tenant,
        policy: Policy,
        gate: PolicyGate,
        logger: RunLogger,
        session: BrowserSession,
        values: dict[str, str],
        *,
        recoveries: list[Recovery],
        handoffs: list[Handoff],
        confirmed: bool,
        handoff_timeout_s: float,
        operator: OperatorServer | None = None,
    ) -> None:
        self.artifact = artifact
        self.tenant = tenant
        self.logger = logger
        self.session = session
        self.values = values  # resolved template values by step id. NEVER logged
        self.recoveries = recoveries
        self.handoffs = handoffs
        self.handoff_timeout_s = handoff_timeout_s
        self.poll_ms = policy.limits.poll_interval_ms
        self.targets = dict(artifact.targets)
        self.provider = SessionProvider(tenant)
        self.control = SessionControl(logger.run_id)
        self.surface = PlaywrightWebSurface(
            session.page,
            policy,
            gate,
            logger,
            mode="replay",
            confirmed=confirmed,
            targets=self.targets,
            mask_selectors=tenant.mask_selectors,
            control=self.control,
        )
        self.operator = operator
        # Only with an operator: someone can take control, so their actions must be captured.
        self.recorder = (
            HumanRecorder(session.context, self.control, logger, mask_selectors=tenant.mask_selectors)
            if operator is not None
            else None
        )
        self.human_actions = self.recorder.actions if self.recorder else []
        self.resyncs = 0  # hand-backs so far (bounded by _MAX_RESYNCS)
        self.acted = False  # has the CURRENT step's action been attempted? (resync may retry it if not)
        self.attempts: Counter[str] = Counter()  # recovery attempts per condition, per run
        self.outputs: dict[str, Scalar] = {}
        self.output_specs = {o.name: o for o in artifact.contract.outputs}

    # ---- top level ------------------------------------------------------------------------------ #

    async def go(self, fault: str | None) -> tuple[str, dict[str, Scalar]]:
        if self.recorder is not None:
            await self.recorder.install()  # before login: every document gets the listener
        await self._login(fault)
        await self._fingerprint()
        steps = self.artifact.steps
        i = 0
        while i < len(steps):  # index-based: a resolved handoff resumes at the resynced step
            try:
                await self._run_step(steps[i])
                i += 1
            except _Resume as r:
                i = r.index
        return await self._final_check()

    async def snapshot(self, reason: str, *, dom: bool = False) -> list[str]:
        """Masked evidence; a broken page must not turn a known failure into a crash."""
        try:
            return await self.surface.snapshot(reason, dom=dom)
        except PlaywrightError as e:
            self.logger.event("snapshot_failed", reason=reason, error=type(e).__name__)
            return []

    async def _login(self, fault: str | None) -> None:
        if fault:
            await self.session.context.add_cookies(
                [{"name": FAULT_COOKIE, "value": fault, "url": self.tenant.base_url}]
            )
            self.logger.event("fault_injected", fault=fault)
        try:
            await self.provider.login(self.session)
        except (LoginFailed, MissingCredentials) as e:
            evidence = await self.snapshot("login_failed")
            self._fail(
                None, FailureCategory.SESSION_EXPIRED, "an authenticated console session", str(e), evidence
            )
        self.logger.event("login_ok")

    async def _fingerprint(self) -> None:
        fp = self.artifact.app.fingerprint

        async def holds() -> bool:
            return await evaluate(self.surface, fp, self.targets)

        if not await poll_until(holds, fp.timeout_ms, self.poll_ms):
            evidence = await self.snapshot("fingerprint", dom=True)
            self._fail(
                None,
                FailureCategory.APP_VERSION_MISMATCH,
                f"{self.artifact.app.product} {self.artifact.app.version_range}: {_describe(fp)}",
                "fingerprint did not match",
                evidence,
            )
        self.logger.event("fingerprint_ok")

    async def _final_check(self) -> tuple[str, dict[str, Scalar]]:
        success = self.artifact.success

        async def holds() -> bool:
            return await evaluate(self.surface, success, self.targets)

        if not await poll_until(holds, success.timeout_ms, self.poll_ms):
            evidence = await self.snapshot("success_check", dom=True)
            self._fail(
                None,
                FailureCategory.CHECKPOINT_FAILED,
                _describe(success),
                "final success check did not hold",
                evidence,
            )
        outcome = next(o for o in self.artifact.contract.outcomes if o.kind is OutcomeKind.success)
        missing = sorted(set(outcome.returns) - set(self.outputs))
        if missing:
            self._fail(
                None,
                FailureCategory.CHECKPOINT_FAILED,
                f"outputs {outcome.returns}",
                f"missing {missing}",
                [],
            )
        self.logger.event("success_check_ok", outcome_code=outcome.code)
        return outcome.code, dict(self.outputs)

    # ---- steps ---------------------------------------------------------------------------------- #

    async def _run_step(self, step: Step) -> None:
        self.logger.event(
            "step_started", step.id, intent=step.intent, action=step.action.type, risk=step.risk.value
        )
        self.acted = False
        await self._perform(step)
        while True:
            t0 = time.monotonic()
            result = await race(
                self.surface, step, self.artifact.conditions, self.targets, poll_interval_ms=self.poll_ms
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if isinstance(result, Satisfied):
                self.logger.event("race_result", step.id, result="satisfied", elapsed_ms=elapsed_ms)
                await self.snapshot(step.id)
                return
            if isinstance(result, Matched):
                self.logger.event(
                    "race_result", step.id, result=f"condition:{result.condition_id}", elapsed_ms=elapsed_ms
                )
                if await self._handle(step, result.condition_id) == "act":
                    await self._perform(step)
                continue
            self.logger.event("race_result", step.id, result="timeout", elapsed_ms=elapsed_ms)
            await self._escalate(
                step,
                FailureCategory.UNKNOWN_STATE,
                "screen matches neither the checkpoint nor any known condition",
            )

    async def _perform(self, step: Step) -> None:
        """Resolve the target (bounded wait) and act or read. Retried only after a recovery."""
        action = step.action
        target_id: str | None = getattr(action, "target", None)
        resolved: Resolved | None = None
        while target_id is not None:
            r = await resolve_target(
                self.surface,
                target_id,
                self.targets[target_id],
                timeout_ms=step.timeout_ms,
                poll_interval_ms=self.poll_ms,
            )
            if isinstance(r, Resolved):
                resolved = r
                break
            # A known condition (popup, error page) may explain the missing target: classify first.
            everywhere = [(cid, c) for cid, c in self.artifact.conditions.items() if c.applies_to == "all"]
            cid = await match_condition(self.surface, everywhere, self.targets, poll_interval_ms=self.poll_ms)
            if cid is None:
                await self._escalate(step, r.category, r.observed, expected=r.expected)
            await self._handle(step, cid)  # recoveries return; the action has not run yet, so resolve again

        self.control.ensure_automation()
        self.acted = True  # attempted counts: a failed click may still have reached the app
        try:
            if isinstance(action, Extract):
                assert resolved is not None
                self._store(step, action, await self.surface.read(resolved))
            else:
                await self.surface.act(action, resolved, step.risk, value=self.values.get(step.id))
        except (PolicyViolation, NeedsHuman) as e:
            self._fail(step, FailureCategory.POLICY_VIOLATION, "an action allowed by policy", str(e), [])
        except ActionFailed as e:
            await self._escalate(step, FailureCategory.TIMEOUT, str(e))
        except NotInControl:
            raise  # internal error: automation must never act while a human holds control

    def _store(self, step: Step, action: Extract, text: str) -> None:
        spec = self.output_specs[action.output]
        expected_type = _PARSE_TYPE[action.parse]
        if spec.type is not expected_type:
            self._fail(
                step,
                FailureCategory.CHECKPOINT_FAILED,
                f"'{action.output}' of type {spec.type.value}",
                f"extract parses as {expected_type.value}",
                [],
            )
        try:
            value = _scalar(parse_value(text, action.parse))
        except ParseError:
            # never echo the text: it may be a balance
            self._fail(
                step,
                FailureCategory.CHECKPOINT_FAILED,
                f"'{action.output}' parseable as {action.parse}",
                f"text of length {len(text)} did not parse",
                [],
            )
        self.outputs[action.output] = value
        self.logger.event(
            "extracted", step.id, output=action.output, value=redact(value, spec.sensitivity, action.output)
        )

    # ---- handlers ------------------------------------------------------------------------------- #

    async def _handle(self, step: Step, cid: str) -> Literal["race", "act"]:
        """Apply a matched condition's handler. Returns what to do next, or raises _Stop."""
        cond = self.artifact.conditions[cid]
        h = cond.handler
        self.logger.event(
            "condition_matched",
            step.id,
            condition=cid,
            classification=cond.classification.value,
            handler=h.do,
        )
        if isinstance(h, ReturnOutcome):
            await self.snapshot(f"{step.id}_{cid}")
            raise _Stop(RunStatus.business_outcome, outcome_code=h.outcome)
        if isinstance(h, Fail):
            evidence = await self.snapshot(f"{step.id}_{cid}", dom=True)
            self._fail(step, h.category, _describe(step.expect), cond.description, evidence)
        if isinstance(h, Escalate):
            await self._escalate(step, h.category, h.reason, observed=cond.description)

        if isinstance(h, Dismiss):
            attempt = await self._next_attempt(step, cid, h.max_times, FailureCategory.RECOVERY_EXHAUSTED)
            r = await resolve_target(
                self.surface,
                h.target,
                self.targets[h.target],
                timeout_ms=step.timeout_ms,
                poll_interval_ms=self.poll_ms,
            )
            if not isinstance(r, Resolved):
                await self._escalate(step, r.category, r.observed, expected=r.expected)
            try:
                await self.surface.act(Click(target=h.target), r, RiskClass.read, value=None)
            except (PolicyViolation, NeedsHuman) as e:
                self._fail(step, FailureCategory.POLICY_VIOLATION, "an action allowed by policy", str(e), [])
            except ActionFailed as e:
                await self._escalate(step, FailureCategory.TIMEOUT, str(e))
            # Dismissed only once the condition is gone: re-racing on the old page would see it again.
            ok = await poll_until(self._cleared(cond), step.timeout_ms, self.poll_ms)
            self._recovered(step, cid, "dismiss", attempt, ok)
            return "race"

        if isinstance(h, WaitUntilClear):
            attempt = await self._next_attempt(step, cid, h.max_attempts, FailureCategory.RECOVERY_EXHAUSTED)
            ok = await poll_until(self._cleared(cond), h.backoff_ms, self.poll_ms)
            self._recovered(step, cid, "wait_until_clear", attempt, ok)
            return "race"  # never repeats the action: the app already accepted it

        assert isinstance(h, Reauthenticate)
        attempt = await self._next_attempt(step, cid, h.max_attempts, FailureCategory.SESSION_EXPIRED)
        if step.risk is RiskClass.irreversible:
            await self._escalate(
                step, FailureCategory.SESSION_EXPIRED, "session expired during an irreversible step"
            )
        self.control.ensure_automation()
        ok = await self.provider.reauthenticate(self.session)
        self._recovered(step, cid, "reauthenticate", attempt, ok)
        if not ok:
            await self._escalate(step, FailureCategory.SESSION_EXPIRED, "re-authentication failed")
        return "act"  # back on the console: perform the current step again

    def _cleared(self, cond: KnownCondition) -> Callable[[], Awaitable[bool]]:
        async def cleared() -> bool:
            return not await evaluate(self.surface, cond.detect, self.targets)

        return cleared

    async def _next_attempt(self, step: Step, cid: str, budget: int, exhausted: FailureCategory) -> int:
        self.attempts[cid] += 1
        if self.attempts[cid] > budget:
            await self._escalate(
                step, exhausted, f"condition '{cid}' persisted after {budget} recovery attempt(s)"
            )
        return self.attempts[cid]

    def _recovered(
        self,
        step: Step,
        cid: str,
        action: Literal["dismiss", "wait_until_clear", "reauthenticate"],
        attempt: int,
        ok: bool,
    ) -> None:
        self.recoveries.append(
            Recovery(step_id=step.id, condition_id=cid, action=action, attempt=attempt, succeeded=ok)
        )
        self.logger.event("recovery", step.id, condition=cid, action=action, attempt=attempt, succeeded=ok)

    # ---- terminal routing (see the routing principle at the top of this module) ---------------- #

    def _fail(
        self,
        step: Step | None,
        category: FailureCategory,
        expected: str,
        observed: str,
        evidence: list[str],
    ) -> NoReturn:
        raise _Stop(
            RunStatus.failed,
            failure=Failure(
                category=category,
                step_id=step.id if step else None,
                step_intent=step.intent if step else None,
                expected=expected,
                observed=observed,
                retryable=category in _RETRYABLE,
                evidence=evidence,
            ),
        )

    async def _escalate(
        self,
        step: Step,
        category: FailureCategory,
        reason: str,
        *,
        expected: str | None = None,
        observed: str | None = None,
    ) -> NoReturn:
        """Snapshot, pause automation on the SAME session, wait for a human (bounded), then resync.

        Raises _Resume(index) when the run can continue, otherwise _Stop (failed). See the module doc.
        """
        evidence = await self.snapshot(f"{step.id}_escalation", dom=True)
        expected = expected or _describe(step.expect)
        request = InterventionRequest(
            intervention_id=f"int_{uuid4().hex[:8]}",
            run_id=self.logger.run_id,
            mode="replay",
            capability_id=self.artifact.id,
            step_id=step.id,
            step_intent=step.intent,
            reason=reason,
            category=category.value,
            current_url=_safe_url(self.session.page.url),
            expected_state=expected,
            screenshot=evidence[0] if evidence else None,
            requested_at=datetime.now(UTC),
        )
        self.logger.event(
            "escalation_requested",
            step.id,
            intervention_id=request.intervention_id,
            category=category.value,
            reason=reason,
            expected_state=expected,
            timeout_s=self.handoff_timeout_s if self.recorder else 0,
        )

        def end(
            resolution: Literal["completed_by_human", "aborted", "timed_out"],
            detail: str,
            fail_as: tuple[FailureCategory, str, str] | None = None,
        ) -> NoReturn:
            self._record_handoff(step, first, history_from, actions_from, resolution, detail=detail)
            cat, exp, obs = fail_as or (category, expected, observed or reason)
            self._fail(step, cat, exp, obs, evidence)

        first = request  # the handoff record keeps the original reason and time; re-asks update `request`
        history_from, actions_from = len(self.control.history), len(self.human_actions)
        if self.recorder is None:
            end("aborted", "no operator available")
        if self.resyncs >= _MAX_RESYNCS:
            end("aborted", f"hand-back budget used up ({_MAX_RESYNCS} per run)")
        await self.recorder.arm()  # documents opened before install listen too

        k = self.artifact.steps.index(step)
        while True:
            try:
                await self.control.request_intervention(request, self.handoff_timeout_s)
            except HandoffAborted as e:
                timed_out = "timed out" in str(e)
                end("timed_out" if timed_out else "aborted", str(e))
            self.resyncs += 1
            index = await self._resync(k)
            if index is not None:
                break
            if self.resyncs >= _MAX_RESYNCS:
                end("aborted", f"no checkpoint held after {self.resyncs} hand-back(s)")
            # Ask again, saying exactly what automation needs to see (RESUMING → PAUSED).
            evidence = await self.snapshot(f"{step.id}_resync_failed", dom=True)
            request = request.model_copy(
                update={
                    "reason": "after hand-back the screen matches no checkpoint from this step on: "
                    "bring it to the expected state, then hand back",
                    "expected_state": self._expected_from(k),
                    "current_url": _safe_url(self.session.page.url),
                    "screenshot": evidence[0] if evidence else None,
                    "requested_at": datetime.now(UTC),
                }
            )
            self.logger.event(
                "resync_failed", step.id, hand_backs=self.resyncs, expected_state=request.expected_state
            )

        # Steps the run will not perform itself. A human must never have committed one for us.
        skipped = self.artifact.steps[k + (1 if self.acted else 0) : index]
        committed = next((s for s in skipped if s.risk is RiskClass.irreversible), None)
        if committed is not None:
            end(
                "completed_by_human",
                f"irreversible step '{committed.id}' was completed by a human",
                (
                    FailureCategory.POLICY_VIOLATION,
                    f"automation performs irreversible step '{committed.id}' itself",
                    "after hand-back its checkpoint already held: a human completed it",
                ),
            )
        self.control.resumed()
        resumed_at = self.artifact.steps[index].id if index < len(self.artifact.steps) else None
        self._record_handoff(step, first, history_from, actions_from, "resumed", resumed_at_step=resumed_at)
        await self.snapshot(f"{step.id}_resumed")
        raise _Resume(index)

    async def _resync(self, k: int) -> int | None:
        """After hand-back: index to continue at, or None if automation can't tell where it is.

        Never trusts "I fixed it": the furthest step j >= k whose `expect` checkpoint holds → j + 1.
        None holds → retry step k if its action never ran, else None (ask the human again).
        """
        await self.surface.settle(_RESYNC_WAIT_MS)
        steps = self.artifact.steps
        found: int | None = None

        async def furthest() -> bool:
            nonlocal found
            for j in range(len(steps) - 1, k - 1, -1):
                expect = steps[j].expect
                if expect is not None and await evaluate(self.surface, expect, self.targets):
                    found = j
                    return True
            return False

        await poll_until(furthest, _RESYNC_WAIT_MS, self.poll_ms)
        index = found + 1 if found is not None else (None if self.acted else k)
        self.logger.event(
            "resync",
            steps[k].id,
            checkpoint=steps[found].id if found is not None else None,
            resume_at=(steps[index].id if index < len(steps) else "success_check")
            if index is not None
            else None,
        )
        return index

    def _expected_from(self, k: int) -> str:
        """The nearest checkpoint automation can resume from, in words (for the operator)."""
        nxt = next((s for s in self.artifact.steps[k:] if s.expect is not None), None)
        if nxt is None:
            return f"final success check: {_describe(self.artifact.success)}"
        return f"checkpoint of step '{nxt.id}' ({nxt.intent}): {_describe(nxt.expect)}"

    def _record_handoff(
        self,
        step: Step,
        request: InterventionRequest,
        history_from: int,
        actions_from: int,
        resolution: Literal["resumed", "completed_by_human", "aborted", "timed_out"],
        *,
        resumed_at_step: str | None = None,
        detail: str | None = None,
    ) -> None:
        history = self.control.history[history_from:]
        taken = [(ts, h) for ts, s, h in history if s is ControlState.HUMAN]
        returned = [ts for ts, s, _ in history if s is ControlState.RESUMING]
        self.handoffs.append(
            Handoff(
                intervention_id=request.intervention_id,
                reason=request.reason,
                step_id=step.id,
                operator_id=taken[-1][1].removeprefix("human:") if taken else None,
                requested_at=request.requested_at,
                taken_at=taken[0][0] if taken else None,
                returned_at=returned[-1] if returned else None,
                resolution=resolution,
                resumed_at_step=resumed_at_step,
                human_actions=self.human_actions[actions_from:],
            )
        )
        self.logger.event(
            "handoff_resolved",
            step.id,
            intervention_id=request.intervention_id,
            resolution=resolution,
            resumed_at_step=resumed_at_step,
            detail=detail,
            human_actions=len(self.human_actions) - actions_from,
        )
