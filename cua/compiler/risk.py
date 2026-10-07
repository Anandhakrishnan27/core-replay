"""Compiler pass: risk. Per-step RiskClass, then policy.max_risk and requires_confirmation.

    fill / select              → reversible (draft state the app has not committed)
    click / press on a control whose name, text or label matches policy.risk.irreversible_button_names
                               → irreversible (LLM and human steps alike)
    everything else            → read
The artifact validator rejects an under-declared policy, and pre-flight refuses an irreversible
capability without the caller's confirmation.
"""

from __future__ import annotations

from cua.safety.policy import PolicyGate
from cua.schema.artifact import CapabilityPolicy, RiskClass
from cua.schema.trace import TraceAction


def step_risk(action: TraceAction, gate: PolicyGate) -> RiskClass:
    if action.tool in ("fill", "select"):
        return RiskClass.reversible
    if action.tool in ("click", "press") and action.element is not None:
        el = action.element
        if any(gate.is_irreversible_name(t) for t in (el.accessible_name, el.text, el.label) if t):
            return RiskClass.irreversible
    return RiskClass.read


def capability_policy(risks: list[RiskClass]) -> CapabilityPolicy:
    top = max(risks, key=lambda r: r.rank, default=RiskClass.read)
    return CapabilityPolicy(max_risk=top, requires_confirmation=top is RiskClass.irreversible)
