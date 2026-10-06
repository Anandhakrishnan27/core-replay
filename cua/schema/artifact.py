"""Capability artifact schema, v1.

An artifact is a *capability*: a CONTRACT (typed inputs, typed outputs,
declared outcomes) plus an IMPLEMENTATION (targets, steps, known conditions)
against one application surface.

    - A calling AI agent reads only `contract` (and `policy`).
    - The replay engine reads everything.
    - A human reviewer reads `intent` / `description` / `rationale` fields,
      which exist specifically so the file is reviewable in a PR diff.

Design rules enforced by validators at the bottom of this file:
    1. Steps reference targets by id; targets are defined once. Tenant
       overrides patch `targets` without touching `steps`.
    2. Every target has at least one *semantic* locator (role / label / text /
       table_cell / near_text). CSS / XPath / coordinates are fallbacks only.
    3. Typed values are never baked in: `fill` values must be templates
       (`{{inputs.x}}`, `{{tenant.x}}`, `{{secrets.x}}`). No PII in artifacts.
    4. Every outcome code a handler can return is declared in the contract.
    5. Condition classification must agree with its handler
       (business -> return_outcome, recoverable -> dismiss/retry,
       hard_failure -> fail/escalate).
    6. Any irreversible step forces `policy.requires_confirmation`.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Final = "1.0"

_SLUG = r"^[a-z][a-z0-9_]*$"
_CAPABILITY_ID = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"  # e.g. mockbank.member.lookup_savings_balance
_SEMVER = r"^\d+\.\d+\.\d+$"
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|tenant|secrets)\.([a-z][a-z0-9_]*)\s*\}\}")
_PURE_TEMPLATE_RE = re.compile(r"^\s*\{\{\s*(inputs|tenant|secrets)\.[a-z][a-z0-9_]*\s*\}\}\s*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Contract: what the caller sees
# --------------------------------------------------------------------------- #


class ValueType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"


class Sensitivity(str, Enum):
    public = "public"  # may be logged verbatim
    internal = "internal"  # may be logged; never a literal in an artifact
    pii = "pii"  # hashed/masked in logs, screenshots and evidence
    secret = "secret"  # never logged or persisted; resolved from a vault at runtime


class InputSpec(_Model):
    name: str = Field(pattern=_SLUG)
    type: ValueType
    description: str
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.internal
    pattern: str | None = Field(
        default=None, description="Regex checked BEFORE touching the UI -> INPUT_INVALID on mismatch."
    )
    example: str | None = Field(default=None, description="Must be synthetic. Shown to reviewers/agents.")


class OutputSpec(_Model):
    name: str = Field(pattern=_SLUG)
    type: ValueType
    description: str
    sensitivity: Sensitivity = Sensitivity.internal


class OutcomeKind(str, Enum):
    success = "success"
    business = "business"  # a legitimate answer the caller must handle, NOT an error


class OutcomeSpec(_Model):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    kind: OutcomeKind
    description: str
    returns: list[str] = Field(default_factory=list, description="Output names populated for this outcome.")


class Contract(_Model):
    inputs: list[InputSpec]
    outputs: list[OutputSpec]
    outcomes: list[OutcomeSpec] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Targets: how a control is found (the robustness story lives here)
# --------------------------------------------------------------------------- #


class RoleLocator(_Model):
    """Accessibility role + accessible name. Works for web AND desktop (UIA/AX)."""

    strategy: Literal["role"] = "role"
    role: str
    name: str | None = None
    exact: bool = True


class LabelLocator(_Model):
    """Form control by its associated <label> / accessible label."""

    strategy: Literal["label"] = "label"
    label: str
    exact: bool = False


class TextLocator(_Model):
    strategy: Literal["text"] = "text"
    text: str
    exact: bool = False


class TableCellLocator(_Model):
    """Legacy-table friendly: the cell in the row containing X, under column header Y.

    Survives column reordering and row reordering; needs no ids or classes.
    """

    strategy: Literal["table_cell"] = "table_cell"
    table_has_header: str
    row_contains: str
    column_header: str


class NearTextLocator(_Model):
    """For unlabeled legacy inputs: the input that follows / sits right of some visible text."""

    strategy: Literal["near_text"] = "near_text"
    anchor_text: str
    relation: Literal["following_input", "right_of", "below"]
    role: str | None = None


class CssLocator(_Model):
    strategy: Literal["css"] = "css"
    selector: str


class XPathLocator(_Model):
    strategy: Literal["xpath"] = "xpath"
    xpath: str


class CoordinateLocator(_Model):
    """Last resort (e.g. canvas/Citrix). Normalized to viewport. Must pass MatchRule checks."""

    strategy: Literal["coordinates"] = "coordinates"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


Locator = Annotated[
    (
        RoleLocator
        | LabelLocator
        | TextLocator
        | TableCellLocator
        | NearTextLocator
        | CssLocator
        | XPathLocator
        | CoordinateLocator
    ),
    Field(discriminator="strategy"),
]

SEMANTIC_STRATEGIES = {"role", "label", "text", "table_cell", "near_text"}


class MatchRule(_Model):
    """Verifies the resolved element really is the intended one (guards against 'found the wrong thing')."""

    unique: bool = True
    visible: bool = True
    enabled: bool | None = None
    text_pattern: str | None = Field(default=None, description="Regex the element's text/value must match.")


class Target(_Model):
    description: str
    locators: list[Locator] = Field(
        min_length=1, description="Tried IN ORDER; first one that resolves AND passes `match` wins."
    )
    frame_path: list[str] = Field(
        default_factory=list, description="Frame names/url-fragments to descend (legacy framesets/iframes)."
    )
    match: MatchRule = MatchRule()
    sensitive: bool = Field(default=False, description="Masked in screenshots and evidence.")
    rationale: str = Field(description="Why these locators, in this order, are robust. For reviewers.")

    @model_validator(mode="after")
    def _has_semantic_locator(self) -> Target:
        if not any(loc.strategy in SEMANTIC_STRATEGIES for loc in self.locators):
            raise ValueError(
                f"target '{self.description}': needs at least one semantic locator "
                f"({sorted(SEMANTIC_STRATEGIES)}); css/xpath/coordinates are fallbacks only"
            )
        return self


# --------------------------------------------------------------------------- #
# Checks: checkpoints (postconditions) and condition detectors
# --------------------------------------------------------------------------- #


class UrlMatches(_Model):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class TargetVisible(_Model):
    kind: Literal["target_visible"] = "target_visible"
    target: str


class TargetAbsent(_Model):
    kind: Literal["target_absent"] = "target_absent"
    target: str


class TextPresent(_Model):
    kind: Literal["text_present"] = "text_present"
    text: str
    within: str | None = Field(default=None, description="Optional target id to scope the search.")


Predicate = Annotated[UrlMatches | TargetVisible | TargetAbsent | TextPresent, Field(discriminator="kind")]


class Check(_Model):
    all_of: list[Predicate] = Field(default_factory=list)
    any_of: list[Predicate] = Field(default_factory=list)
    timeout_ms: int = Field(default=10_000, gt=0)

    @model_validator(mode="after")
    def _non_empty(self) -> Check:
        if not self.all_of and not self.any_of:
            raise ValueError("a check needs at least one predicate")
        return self

    def predicates(self) -> list:
        return [*self.all_of, *self.any_of]


# --------------------------------------------------------------------------- #
# Known conditions: the runtime error taxonomy, declared per capability
# --------------------------------------------------------------------------- #


class FailureCategory(str, Enum):
    INPUT_INVALID = "INPUT_INVALID"  # caller error; UI never touched
    POLICY_VIOLATION = "POLICY_VIOLATION"  # guardrail blocked an action
    APP_VERSION_MISMATCH = "APP_VERSION_MISMATCH"  # fingerprint didn't match app binding
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    UNKNOWN_STATE = "UNKNOWN_STATE"  # screen matches neither checkpoint nor any known condition
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    APP_ERROR = "APP_ERROR"
    TIMEOUT = "TIMEOUT"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"  # a recoverable condition kept recurring


class ReturnOutcome(_Model):
    do: Literal["return_outcome"] = "return_outcome"
    outcome: str


class Dismiss(_Model):
    do: Literal["dismiss"] = "dismiss"
    target: str
    max_times: int = Field(default=2, ge=1)


class RetryStep(_Model):
    do: Literal["retry_step"] = "retry_step"
    max_attempts: int = Field(default=3, ge=1)
    backoff_ms: int = Field(default=1_000, ge=0)


class Reauthenticate(_Model):
    """Re-login via the session provider (credentials from env), then retry the step.

    If re-auth fails or the condition persists after `max_attempts`, the executor escalates
    with category SESSION_EXPIRED.
    """

    do: Literal["reauthenticate"] = "reauthenticate"
    max_attempts: int = Field(default=1, ge=1, le=2)


class Fail(_Model):
    do: Literal["fail"] = "fail"
    category: FailureCategory


class Escalate(_Model):
    do: Literal["escalate"] = "escalate"
    category: FailureCategory = Field(description="Reported if the human aborts instead of resolving.")
    reason: str


Handler = Annotated[
    ReturnOutcome | Dismiss | RetryStep | Reauthenticate | Fail | Escalate, Field(discriminator="do")
]


class ConditionClass(str, Enum):
    business_outcome = "business_outcome"
    recoverable = "recoverable"
    hard_failure = "hard_failure"


_ALLOWED_HANDLERS = {
    ConditionClass.business_outcome: {"return_outcome"},
    ConditionClass.recoverable: {"dismiss", "retry_step", "reauthenticate"},
    ConditionClass.hard_failure: {"fail", "escalate"},
}


class KnownCondition(_Model):
    classification: ConditionClass
    description: str
    detect: Check
    handler: Handler
    applies_to: Literal["all"] | list[str] = Field(
        default="all", description="'all' or a list of step ids after which to watch for this condition."
    )

    @model_validator(mode="after")
    def _class_matches_handler(self) -> KnownCondition:
        if self.handler.do not in _ALLOWED_HANDLERS[self.classification]:
            raise ValueError(
                f"condition '{self.description}': {self.classification.value} cannot use "
                f"handler '{self.handler.do}' (allowed: {sorted(_ALLOWED_HANDLERS[self.classification])})"
            )
        return self


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


class Navigate(_Model):
    type: Literal["navigate"] = "navigate"
    url: str = Field(
        description="Template, e.g. '{{tenant.base_url}}/members/search'. Never a tenant literal."
    )


class Click(_Model):
    type: Literal["click"] = "click"
    target: str


class Fill(_Model):
    type: Literal["fill"] = "fill"
    target: str
    value: str = Field(description="Must be a single template: {{inputs.x}} | {{tenant.x}} | {{secrets.x}}")


class SelectOption(_Model):
    type: Literal["select"] = "select"
    target: str
    option: str


class Press(_Model):
    type: Literal["press"] = "press"
    key: str
    target: str | None = None


class Extract(_Model):
    type: Literal["extract"] = "extract"
    target: str
    output: str
    parse: Literal["text", "integer", "decimal", "currency", "date"] = "text"


Action = Annotated[Navigate | Click | Fill | SelectOption | Press | Extract, Field(discriminator="type")]


class RiskClass(str, Enum):
    read = "read"  # observe / navigate / extract
    reversible = "reversible"  # fills, draft state, cancellable dialogs
    irreversible = "irreversible"  # commits: open account, post transfer, delete

    @property
    def rank(self) -> int:
        return {"read": 0, "reversible": 1, "irreversible": 2}[self.value]


class Step(_Model):
    id: str = Field(pattern=_SLUG)
    intent: str = Field(
        description="Human-readable purpose. Also the context given to a human on escalation."
    )
    action: Action
    risk: RiskClass = RiskClass.read
    expect: Check | None = Field(
        default=None, description="Checkpoint: postcondition asserted after the action."
    )
    timeout_ms: int = Field(default=10_000, gt=0)


# --------------------------------------------------------------------------- #
# Binding, policy, provenance, review
# --------------------------------------------------------------------------- #


class AppBinding(_Model):
    """Binds the capability to a VENDOR PRODUCT + version range, not to a tenant."""

    product: str = Field(pattern=_SLUG)
    version_range: str = Field(description="PEP 440-style specifier, e.g. '>=2.0,<3.0'.")
    surface: Literal["web", "legacy_web", "desktop"]
    fingerprint: Check = Field(description="Proves we are on the expected product/version before step 1.")


class CapabilityPolicy(_Model):
    max_risk: RiskClass = Field(description="Declared highest step risk; validated against the steps.")
    requires_confirmation: bool = Field(description="Caller must pass an explicit confirmation to run.")


class Provenance(_Model):
    discovery_run_id: str
    model: str
    recorded_at: datetime
    compiler_version: str
    human_assisted_steps: list[str] = Field(
        default_factory=list, description="Step ids that came from a human during a handoff."
    )


class ReviewStatus(str, Enum):
    draft = "draft"  # replay allowed only in supervised mode
    approved = "approved"  # unattended replay allowed
    deprecated = "deprecated"


class Review(_Model):
    status: ReviewStatus = ReviewStatus.draft
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    notes: str | None = None


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #


class CapabilityArtifact(_Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=_CAPABILITY_ID)
    version: str = Field(pattern=_SEMVER)
    title: str
    description: str

    app: AppBinding
    contract: Contract
    policy: CapabilityPolicy

    targets: dict[str, Target]
    conditions: dict[str, KnownCondition] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    success: Check = Field(description="Final checkpoint that must hold before returning SUCCESS.")

    provenance: Provenance
    review: Review = Review()

    # ---- cross-reference validation ------------------------------------- #

    @model_validator(mode="after")
    def _validate_references(self) -> CapabilityArtifact:
        errors: list[str] = []
        target_ids = set(self.targets)
        step_ids = [s.id for s in self.steps]
        input_names = {i.name for i in self.contract.inputs}
        output_names = {o.name for o in self.contract.outputs}
        outcome_codes = {o.code for o in self.contract.outcomes}

        for tid in target_ids:
            if not re.match(_SLUG, tid):
                errors.append(f"target id '{tid}' must match {_SLUG}")

        if len(step_ids) != len(set(step_ids)):
            errors.append("step ids must be unique")

        def check_target(ref: str | None, where: str) -> None:
            if ref is not None and ref not in target_ids:
                errors.append(f"{where}: unknown target '{ref}'")

        def check_predicates(check: Check | None, where: str) -> None:
            if check is None:
                return
            for p in check.predicates():
                check_target(getattr(p, "target", None), where)
                check_target(getattr(p, "within", None), where)

        def check_templates(text: str, where: str) -> None:
            for scope, name in TEMPLATE_RE.findall(text):
                if scope == "inputs" and name not in input_names:
                    errors.append(f"{where}: template references undeclared input '{name}'")

        # Outcomes
        if not any(o.kind is OutcomeKind.success for o in self.contract.outcomes):
            errors.append("contract must declare at least one success outcome")
        for o in self.contract.outcomes:
            for r in o.returns:
                if r not in output_names:
                    errors.append(f"outcome {o.code}: returns undeclared output '{r}'")

        # Steps
        extracted: set[str] = set()
        max_rank = 0
        for s in self.steps:
            where = f"step '{s.id}'"
            a = s.action
            check_target(getattr(a, "target", None), where)
            check_predicates(s.expect, where)
            max_rank = max(max_rank, s.risk.rank)
            if isinstance(a, Navigate):
                check_templates(a.url, where)
            if isinstance(a, Fill):
                if not _PURE_TEMPLATE_RE.match(a.value):
                    errors.append(f"{where}: fill value must be a single template, got a literal")
                check_templates(a.value, where)
            if isinstance(a, Extract):
                if a.output not in output_names:
                    errors.append(f"{where}: extracts undeclared output '{a.output}'")
                extracted.add(a.output)
                if s.risk is not RiskClass.read:
                    errors.append(f"{where}: extract steps must be risk 'read'")

        for o in self.contract.outcomes:
            if o.kind is OutcomeKind.success:
                missing = set(o.returns) - extracted
                if missing:
                    errors.append(f"outcome {o.code}: outputs {sorted(missing)} are never extracted")

        # Conditions
        for cid, c in self.conditions.items():
            where = f"condition '{cid}'"
            check_predicates(c.detect, where)
            if isinstance(c.handler, Dismiss):
                check_target(c.handler.target, where)
            if isinstance(c.handler, ReturnOutcome) and c.handler.outcome not in outcome_codes:
                errors.append(f"{where}: returns undeclared outcome '{c.handler.outcome}'")
            if isinstance(c.applies_to, list):
                for sid in c.applies_to:
                    if sid not in step_ids:
                        errors.append(f"{where}: applies_to unknown step '{sid}'")

        check_predicates(self.success, "success")
        check_predicates(self.app.fingerprint, "app.fingerprint")

        # Policy honesty
        if self.policy.max_risk.rank != max_rank:
            actual = next(r for r in RiskClass if r.rank == max_rank)
            errors.append(
                f"policy.max_risk is '{self.policy.max_risk.value}' but the riskiest step is '{actual.value}'"
            )
        if max_rank == RiskClass.irreversible.rank and not self.policy.requires_confirmation:
            errors.append("irreversible steps present: policy.requires_confirmation must be true")

        # Review
        if self.review.status is ReviewStatus.approved and not self.review.reviewed_by:
            errors.append("approved artifacts must name a reviewer")

        if errors:
            raise ValueError("invalid capability artifact:\n  - " + "\n  - ".join(errors))
        return self
