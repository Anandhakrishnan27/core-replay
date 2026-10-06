"""Intervention request: everything a human needs to act, nothing they shouldn't see."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intervention_id: str
    run_id: str
    mode: Literal["discovery", "replay"]
    capability_id: str | None = Field(default=None, description="None during discovery")
    goal: str | None = None
    step_id: str | None = None
    step_intent: str | None = None
    reason: str
    category: str | None = None
    current_url: str
    expected_state: str | None = Field(default=None, description="What automation needs to see to resume")
    screenshot: str | None = Field(default=None, description="Evidence-relative path to a MASKED screenshot")
    requested_at: datetime
