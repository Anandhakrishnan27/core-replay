"""Discovery trace: what the LLM run recorded. Input to the compiler.

The element snapshot is captured AT ACTION TIME, because after the run the page is gone,
and the compiler needs it to generate and verify robust locators.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.schema.artifact import Locator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableContext(_Model):
    table_headers: list[str]
    row_text: str
    column_header: str


class ElementSnapshot(_Model):
    frame_path: list[str] = Field(default_factory=list)
    tag: str
    role: str | None = None
    accessible_name: str | None = None
    label: str | None = None
    name_attr: str | None = None
    text: str | None = Field(default=None, description="Redacted if the element is sensitive.")
    nearby_text: dict[str, str] = Field(default_factory=dict, description="left/above anchor captions")
    table_context: TableContext | None = None
    css_path: str | None = None
    xpath: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    verified_locators: list[Locator] = Field(
        default_factory=list,
        description="Candidate locators that matched exactly this element on the live page at action time, "
        "in rank order. The page is gone after the run, so this is the compiler's only uniqueness evidence.",
    )


class PageState(_Model):
    url: str
    title: str
    headings: list[str] = Field(default_factory=list)
    visible_texts: list[str] = Field(default_factory=list, description="Short, non-data UI strings only.")


class TraceAction(_Model):
    seq: int
    at: datetime
    actor: Literal["llm", "human"]
    tool: Literal["click", "fill", "select", "press", "extract", "dismiss"]
    element: ElementSnapshot | None = None
    value: str | None = Field(
        default=None, description="Literal typed value (redacted in logs, used by compiler)."
    )
    output_name: str | None = None
    page_before: PageState
    page_after: PageState
    ok: bool = True
    error: str | None = None
    reasoning: str | None = Field(default=None, description="Short model rationale, for evidence only.")


class DiscoveryTrace(_Model):
    run_id: str
    goal: str
    goal_values: dict[str, str] = Field(
        default_factory=dict, description="Values mentioned in the goal, e.g. {'member_id': '10001'}."
    )
    tenant_id: str
    model: str
    started_at: datetime
    actions: list[TraceAction] = Field(default_factory=list)
    status: Literal["completed", "escalated", "failed"] = "completed"
    outputs: dict[str, str] = Field(default_factory=dict)
