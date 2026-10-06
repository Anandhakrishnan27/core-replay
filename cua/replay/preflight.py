"""Pre-flight: everything that can be refused BEFORE touching the UI → status `rejected`."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cua.config import Policy
from cua.schema.artifact import CapabilityArtifact, FailureCategory, ReviewStatus, RiskClass, ValueType
from cua.schema.result import Failure

RunMode = str  # "supervised" | "unattended"


@dataclass
class PreflightOk:
    inputs: dict[str, str]


def _type_ok(value: str, t: ValueType) -> bool:
    patterns = {
        ValueType.integer: r"^-?[0-9]+$",
        ValueType.decimal: r"^-?[0-9]+(\.[0-9]+)?$",
        ValueType.boolean: r"^(true|false)$",
        ValueType.date: r"^\d{4}-\d{2}-\d{2}$",
    }
    return t is ValueType.string or re.match(patterns[t], value) is not None


def preflight(
    artifact: CapabilityArtifact,
    inputs: dict[str, str],
    policy: Policy,
    *,
    mode: RunMode = "unattended",
    confirmed: bool = False,
) -> PreflightOk | Failure:
    def reject(category: FailureCategory, expected: str, observed: str) -> Failure:
        return Failure(category=category, expected=expected, observed=observed, retryable=False)

    # 1. review status vs run mode
    if artifact.review.status is ReviewStatus.deprecated:
        return reject(FailureCategory.POLICY_VIOLATION, "a non-deprecated artifact", "deprecated")
    if mode == "unattended" and artifact.review.status.value != policy.unattended_requires_review_status:
        return reject(
            FailureCategory.POLICY_VIOLATION,
            f"review status '{policy.unattended_requires_review_status}' for unattended runs",
            artifact.review.status.value,
        )

    # 2. irreversible capability needs explicit confirmation (decided up front, never mid-run)
    if artifact.policy.max_risk is RiskClass.irreversible and not confirmed:
        return reject(
            FailureCategory.POLICY_VIOLATION,
            "caller confirmation for irreversible capability",
            "no confirmation",
        )

    # 3. inputs vs contract (values are never echoed back: they may be PII)
    declared = {i.name: i for i in artifact.contract.inputs}
    unknown = set(inputs) - set(declared)
    if unknown:
        return reject(
            FailureCategory.INPUT_INVALID, f"inputs {sorted(declared)}", f"unknown inputs {sorted(unknown)}"
        )
    for spec in declared.values():
        if spec.name not in inputs:
            if spec.required:
                return reject(FailureCategory.INPUT_INVALID, f"required input '{spec.name}'", "missing")
            continue
        value = inputs[spec.name]
        if not _type_ok(value, spec.type):
            return reject(
                FailureCategory.INPUT_INVALID,
                f"'{spec.name}' of type {spec.type.value}",
                f"value of length {len(value)} not matching type",
            )
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            return reject(
                FailureCategory.INPUT_INVALID,
                f"'{spec.name}' matching {spec.pattern}",
                f"value of length {len(value)} not matching pattern",
            )
    return PreflightOk(inputs=inputs)
