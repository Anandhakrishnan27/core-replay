"""Trace → CapabilityArtifact. No LLM. Same trace in, same artifact out.

Passes (each in its own module):
  1. clean          drop failed actions, pull out dismissals, last fill/extract wins, cut loops
  2. parameterize   literals → {{inputs.x}} / {{tenant.base_url}}; input types + patterns; outputs
  3. locators       verified candidates → Target (semantic first, positional fallbacks dropped)
  4. checkpoints    page_before/after diffs → postconditions on UI chrome only, fingerprint
  5. conditions     product pack + derived business outcomes + observed interstitials
  6. risk           per-step RiskClass; policy.max_risk; requires_confirmation
  7. validate       CapabilityArtifact.model_validate (cross-references, templates, policy honesty)
The self-test replay (stage 5) runs after this, against the live app, before anything is saved.
Output: an artifact with review.status = draft.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from cua.compiler import CompileError
from cua.compiler.checkpoints import changed, fingerprint, new_heading, step_expect
from cua.compiler.clean import clean, element_key
from cua.compiler.conditions_library import (
    PACK_DIR,
    covered_texts,
    load_pack,
    missing_row,
    observed_interstitial,
    ordered,
    resolve_pack,
)
from cua.compiler.locators import TEXT_PATTERNS, build_target, describe, slug
from cua.compiler.parameterize import (
    entity,
    input_spec,
    output_spec,
    param_for,
    start_url,
    title_from_goal,
)
from cua.compiler.risk import capability_policy, step_risk
from cua.config import Policy, Tenant
from cua.safety.policy import PolicyGate
from cua.schema.artifact import (
    Action,
    AppBinding,
    CapabilityArtifact,
    Click,
    Contract,
    Extract,
    Fill,
    InputSpec,
    KnownCondition,
    Navigate,
    OutcomeKind,
    OutcomeSpec,
    OutputSpec,
    Press,
    Provenance,
    ReturnOutcome,
    Review,
    RiskClass,
    SelectOption,
    Sensitivity,
    Step,
    Target,
    TargetVisible,
    TextPresent,
)
from cua.schema.trace import DiscoveryTrace, TraceAction

COMPILER_VERSION = "0.1.0"
REVIEW_NOTE = (
    "Compiled automatically. Before approving, check: locators and their order, checkpoint texts, "
    "condition texts and outcome codes, input pattern, and output sensitivity."
)


@dataclass
class _Planned:
    action: TraceAction
    step_id: str
    target_id: str | None
    act: Action


def compile_trace(
    trace: DiscoveryTrace,
    capability_id: str,
    version: str = "1.0.0",
    *,
    tenant: Tenant,
    policy: Policy,
    pack_dir: Path = PACK_DIR,
) -> CapabilityArtifact:
    """Raises CompileError (never echoing values) when the trace cannot become a safe artifact."""
    if trace.status != "completed":
        raise CompileError(f"discovery run {trace.run_id} ended '{trace.status}', not 'completed'")
    cleaned = clean(trace)
    path = cleaned.actions
    if not path:
        raise CompileError("the trace has no successful actions to compile")
    gate = PolicyGate(policy)

    targets: dict[str, Target] = {}
    target_by_element: dict[object, str] = {}
    step_ids: set[str] = set()

    def unique(base: str, taken: Container[str]) -> str:
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        return name

    def target_for(a: TraceAction, *, sensitive: bool = False, text_pattern: str | None = None) -> str:
        assert a.element is not None
        key = element_key(a.element)
        if key in target_by_element:
            return target_by_element[key]
        tid = unique(describe(a.element)[0], targets)
        targets[tid] = build_target(
            a.element, sensitive=sensitive, text_pattern=text_pattern, where=f"action {a.seq} ({a.tool})"
        )
        target_by_element[key] = tid
        return tid

    # ---- 2+3. parameterize, outputs, targets, steps --------------------------------------------- #
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}
    planned: list[_Planned] = []
    for a in path:
        tid: str | None
        if a.tool in ("fill", "fill_secret"):
            name = param_for(a, trace)
            inputs.setdefault(name, input_spec(name, a))
            tid = target_for(a, sensitive=inputs[name].sensitivity is not Sensitivity.public)
            act: Action = Fill(target=tid, value="{{inputs." + name + "}}")
            sid = f"enter_{name}"
        elif a.tool == "extract":
            spec, parse = output_spec(a)
            outputs[spec.name] = spec
            pattern = TEXT_PATTERNS.get(parse if parse != "text" else "", None)
            tid = target_for(a, sensitive=spec.sensitivity is Sensitivity.pii, text_pattern=pattern)
            act = Extract(target=tid, output=spec.name, parse=parse)
            sid = f"read_{spec.name}"
        elif a.tool == "select":
            if not a.value or "«" in a.value:
                raise CompileError(f"action {a.seq}: the selected option was redacted; it cannot be replayed")
            tid = target_for(a)
            act = SelectOption(target=tid, option=a.value)
            sid = f"select_{tid}"
        elif a.tool == "press":
            tid = target_for(a) if a.element is not None else None
            act = Press(key=a.value or "", target=tid)
            sid = f"press_{slug(a.value or 'key')}"
        elif a.tool == "click":
            tid = target_for(a)
            act = Click(target=tid)
            sid = f"click_{tid}"
        else:
            raise CompileError(f"action {a.seq}: tool '{a.tool}' cannot be compiled")
        sid = unique(sid, step_ids)
        step_ids.add(sid)
        planned.append(_Planned(a, sid, tid, act))

    start_template, start_path = start_url(path[0])
    open_id = unique(f"open_{slug(start_path.rstrip('/').rsplit('/', 1)[-1] or 'start')}", step_ids)

    # ---- 4. checkpoints ------------------------------------------------------------------------- #
    expects = {}
    headings: dict[str, str] = {}
    for i, p in enumerate(planned):
        a = p.action
        if a.tool not in ("click", "press", "select") or not changed(a.page_before, a.page_after):
            continue
        nxt = next((q.target_id for q in planned[i + 1 :] if q.target_id), None)
        heading = new_heading(a.page_before, a.page_after)
        expects[p.step_id] = step_expect(heading, nxt)
        if heading:
            headings[p.step_id] = heading

    # ---- 5. conditions -------------------------------------------------------------------------- #
    last_fill = max((i for i, p in enumerate(planned) if p.action.tool == "fill"), default=None)
    search_step, searched = None, None
    if last_fill is not None:
        searched = entity(param_for(planned[last_fill].action, trace))
        search_step = next(
            (p.step_id for p in planned[last_fill + 1 :] if p.action.tool in ("click", "press")), None
        )
    pack = resolve_pack(load_pack(tenant.product, pack_dir), entity=searched, search_step=search_step)
    conditions: dict[str, KnownCondition] = dict(pack.conditions)
    for tid, t in pack.targets.items():
        if tid in targets:
            raise CompileError(f"pack target '{tid}' collides with a discovered target")
        targets[tid] = t

    outcomes: list[OutcomeSpec] = []
    by_target = {p.target_id: p for p in planned if p.action.tool == "extract"}
    for sid, check in expects.items():
        if check is None or sid not in headings:
            continue
        for pred in check.all_of:
            if not isinstance(pred, TargetVisible):
                continue
            cell = by_target.get(pred.target)
            if cell is None or targets[pred.target].locators[0].strategy != "table_cell":
                continue
            cid, _, cond = missing_row(sid, headings[sid], pred.target, cell.action)
            conditions.setdefault(cid, cond)

    covered = covered_texts(conditions)
    for d in cleaned.interstitials:
        found = observed_interstitial(d)
        if found is None:
            continue
        cid, tid, target, cond = found
        if any(p.text in covered for p in cond.detect.predicates() if isinstance(p, TextPresent)):
            continue  # the pack already handles this dialog
        targets[unique(tid, targets)] = target
        conditions[unique(cid, conditions)] = cond

    conditions = ordered(conditions)
    for c in conditions.values():
        if isinstance(c.handler, ReturnOutcome) and all(o.code != c.handler.outcome for o in outcomes):
            outcomes.append(
                OutcomeSpec(code=c.handler.outcome, kind=OutcomeKind.business, description=c.description)
            )
    success = OutcomeSpec(
        code="SUCCESS",
        kind=OutcomeKind.success,
        description="Goal completed" + (f"; returns {', '.join(outputs)}." if outputs else "."),
        returns=list(outputs),
    )

    # ---- 6. risk -------------------------------------------------------------------------------- #
    steps = [
        Step(
            id=open_id, intent=f"Open {start_path}", action=Navigate(url=start_template), risk=RiskClass.read
        )
    ]
    for p in planned:
        steps.append(
            Step(
                id=p.step_id,
                intent=_intent(p, targets),
                action=p.act,
                risk=step_risk(p.action, gate),
                expect=expects.get(p.step_id),
            )
        )
    cap_policy = capability_policy([s.risk for s in steps])

    # ---- success check + app binding -------------------------------------------------------------- #
    last_check = next((expects[p.step_id] for p in reversed(planned) if expects.get(p.step_id)), None)
    if last_check is not None:
        success_check = last_check.model_copy(update={"any_of": []})
    else:
        success_check = fingerprint(path[-1].page_after, None)
    major = tenant.product_version.split(".", 1)[0]
    app = AppBinding(
        product=tenant.product,
        version_range=f">={major}.0,<{int(major) + 1}.0"
        if major.isdigit()
        else f"=={tenant.product_version}",
        surface="legacy_web" if any(t.frame_path for t in targets.values()) else "web",
        fingerprint=fingerprint(path[0].page_before, planned[0].target_id),
    )

    title = title_from_goal(trace)
    effect = {
        RiskClass.read: "Read-only.",
        RiskClass.reversible: "Fills forms but commits nothing.",
        RiskClass.irreversible: "COMMITS a change: callers must confirm.",
    }[cap_policy.max_risk]
    data = {
        "id": capability_id,
        "version": version,
        "title": title,
        "description": f"{title}. {effect} Compiled from discovery run {trace.run_id}.",
        "app": app,
        "contract": Contract(
            inputs=list(inputs.values()), outputs=list(outputs.values()), outcomes=[success, *outcomes]
        ),
        "policy": cap_policy,
        "targets": targets,
        "conditions": conditions,
        "steps": steps,
        "success": success_check,
        "provenance": Provenance(
            discovery_run_id=trace.run_id,
            model=trace.model,
            recorded_at=trace.started_at,
            compiler_version=COMPILER_VERSION,
            human_assisted_steps=[p.step_id for p in planned if p.action.actor == "human"],
        ),
        "review": Review(notes=REVIEW_NOTE),
    }
    # ---- 7. validate ---------------------------------------------------------------------------- #
    try:
        return CapabilityArtifact.model_validate(_plain(data))
    except ValidationError as e:
        raise CompileError(f"compiled artifact is invalid: {e}") from None


def _plain(value: Any) -> Any:
    """Models → JSON-ready dicts, so the artifact validates exactly as if loaded from its file."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _intent(p: _Planned, targets: dict[str, Target]) -> str:
    what = targets[p.target_id].description if p.target_id else "the page"
    a = p.act
    if isinstance(a, Fill):
        return f"Type {a.value} into the {what}"
    if isinstance(a, Extract):
        return f"Read {a.output} from the {what}"
    if isinstance(a, SelectOption):
        return f"Choose '{a.option}' in the {what}"
    if isinstance(a, Press):
        return f"Press {a.key}" + (f" in the {what}" if p.target_id else "")
    return f"Click the {what}"
