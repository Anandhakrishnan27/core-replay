"""Run result contract: what a caller (AI agent) gets back from a replay or discovery run.

Four terminal statuses, deliberately distinct:

    success           -> outcome_code is a declared success outcome; outputs populated
    business_outcome  -> outcome_code is a declared business outcome (e.g. MEMBER_NOT_FOUND).
                         Not an error. The agent should branch on it.
    failed            -> `failure` explains step / expected / observed / evidence.
    rejected          -> refused before touching the UI (INPUT_INVALID, POLICY_VIOLATION,
                         unapproved artifact in unattended mode). Safe to fix and retry.

`escalated` is NOT terminal: while a human holds control, the run is suspended.
A run that was escalated ends as one of the four above; `handoffs` records what happened.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifact import FailureCategory

Scalar = str | int | float | bool | None


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatus(str, Enum):
    success = "success"
    business_outcome = "business_outcome"
    failed = "failed"
    rejected = "rejected"


class Recovery(_Model):
    step_id: str
    condition_id: str
    action: Literal["dismiss", "retry_step", "reauthenticate"]
    attempt: int
    succeeded: bool


class Failure(_Model):
    category: FailureCategory
    step_id: str | None = None
    step_intent: str | None = None
    expected: str = Field(description="What the step/checkpoint required, in words.")
    observed: str = Field(description="What was actually on screen (redacted).")
    retryable: bool = Field(description="True if re-invoking later could plausibly succeed (e.g. TIMEOUT).")
    evidence: list[str] = Field(default_factory=list, description="Paths relative to evidence_dir.")


class HumanAction(_Model):
    at: datetime
    kind: Literal["click", "fill", "select", "press", "navigate"]
    target_hint: str = Field(description="Best-effort semantic description, e.g. role=button name='OK'.")
    value: str | None = Field(default=None, description="Always redacted for fill: '«redacted:len=5»'.")


class Handoff(_Model):
    intervention_id: str
    reason: str
    step_id: str | None
    operator_id: str | None
    requested_at: datetime
    taken_at: datetime | None = None
    returned_at: datetime | None = None
    resolution: Literal["resumed", "completed_by_human", "aborted", "timed_out"]
    resumed_at_step: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)


class RunResult(_Model):
    run_id: str
    mode: Literal["discovery", "replay"]
    capability_id: str | None
    capability_version: str | None
    tenant_id: str
    status: RunStatus
    outcome_code: str | None = None
    outputs: dict[str, Scalar] = Field(default_factory=dict)
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    started_at: datetime
    duration_ms: int
    evidence_dir: str

    @model_validator(mode="after")
    def _status_shape(self) -> RunResult:
        if self.status in (RunStatus.success, RunStatus.business_outcome):
            if not self.outcome_code or self.failure is not None:
                raise ValueError(f"{self.status.value} requires outcome_code and no failure")
        else:
            if self.failure is None or self.outputs:
                raise ValueError(f"{self.status.value} requires a failure and no outputs")
        return self
