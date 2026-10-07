"""Compiler pass: parameterize (literals → templates) and outputs (extracts → contract).

  fill literal   → {{inputs.<param>}}   the literal must equal a --param value (raw, or both hashed in a
                                        saved trace); a literal that is not a parameter is a CompileError
  start page     → {{tenant.base_url}}<path>   never a tenant host in the artifact
  extract        → OutputSpec + Extract.parse, from the element's shape token («shape:currency»)

Inputs default to `pii` (customer identifiers), with no `example`: the discovery value might be real.
Outputs whose value was redacted on screen (a shape or «masked») are `pii`; plain UI text is `internal`.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from cua.compiler import CompileError
from cua.compiler.locators import is_ui_text, row_anchor
from cua.schema.artifact import InputSpec, OutputSpec, Sensitivity, ValueType
from cua.schema.trace import DiscoveryTrace, ElementSnapshot, TraceAction

Parse = Literal["text", "integer", "decimal", "currency", "date"]

_SHAPE_RE = re.compile(r"^«shape:([a-z]+)»$")
_SHAPES: dict[str, tuple[Parse, ValueType]] = {
    "currency": ("currency", ValueType.decimal),
    "decimal": ("decimal", ValueType.decimal),
    "integer": ("integer", ValueType.integer),
    "date": ("date", ValueType.date),
}
_HASH_TOKEN_RE = re.compile(r"«([a-z][a-z0-9_]*):sha256:[0-9a-f]+»")
_ID_SUFFIX_RE = re.compile(r"_(id|number|num|no)$")


def param_for(action: TraceAction, trace: DiscoveryTrace) -> str:
    for name, value in trace.goal_values.items():
        if action.value == value:
            return name
    raise CompileError(
        f"action {action.seq}: the typed value is not one of the declared parameters "
        f"{sorted(trace.goal_values)}; re-run discovery with --param name=value"
    )


def caption(el: ElementSnapshot | None) -> str | None:
    """What a human calls this control: its label, the caption beside it, or its accessible name."""
    if el is None:
        return None
    for text in (el.label, el.nearby_text.get("left"), el.accessible_name):
        if is_ui_text(text):
            return text
    return None


def input_spec(name: str, action: TraceAction) -> InputSpec:
    raw = action.value or ""
    hashed = raw.startswith("«")
    where = caption(action.element)
    return InputSpec(
        name=name,
        type=ValueType.string,  # identifiers keep leading zeros: never integer
        description=f"Typed into the '{where}' field." if where else "Typed into a text field.",
        sensitivity=Sensitivity.pii,
        # Digits only when the discovery value was digits. Reviewers tighten the length.
        pattern=r"^[0-9]+$" if raw.isdigit() and not hashed else None,
    )


def output_spec(action: TraceAction) -> tuple[OutputSpec, Parse]:
    name = action.output_name or ""
    el = action.element
    text = (el.text or el.accessible_name or "") if el else ""
    m = _SHAPE_RE.match(text)
    parse, vtype = _SHAPES.get(m.group(1), ("text", ValueType.string)) if m else ("text", ValueType.string)
    sensitivity = Sensitivity.pii if text.startswith("«") else Sensitivity.internal
    ctx = el.table_context if el else None
    anchor = row_anchor(el) if el else None
    if ctx and anchor:
        description = f"'{ctx.column_header}' of the '{anchor}' row."
    else:
        description = f"Value shown in '{caption(el)}'." if caption(el) else "Value shown on screen."
    return OutputSpec(name=name, type=vtype, description=description, sensitivity=sensitivity), parse


def entity(param: str) -> str:
    """member_id → member: names a not-found outcome after what was searched for."""
    return _ID_SUFFIX_RE.sub("", param) or param


def start_url(first: TraceAction) -> tuple[str, str]:
    """(template, path) for the screen discovery started on."""
    path = urlsplit(first.page_before.url).path or "/"
    return "{{tenant.base_url}}" + path, path


def title_from_goal(trace: DiscoveryTrace) -> str:
    """The goal with parameter values (raw or hashed) replaced by <name>, and any other number hidden."""
    goal = trace.goal
    for name, value in trace.goal_values.items():
        if value:
            goal = goal.replace(value, f"<{name}>")
    goal = _HASH_TOKEN_RE.sub(lambda m: f"<{m.group(1)}>", goal)
    goal = re.sub(r"\d{4,}", "<number>", goal)
    goal = goal.strip().rstrip(".")
    return goal[:1].upper() + goal[1:]
