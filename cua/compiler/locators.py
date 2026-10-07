"""Compiler pass: locators. Candidates from an ElementSnapshot, in rank order.

    role → label → text → table_cell → near_text → css

`candidates()` is pure and runs at ACTION time during discovery: Surface.verify_locators() then keeps only
the ones that match exactly the acted element on the live page (ElementSnapshot.verified_locators).
Nothing is built from data: a name or text that is a redaction token («shape:…», «masked») never
becomes a locator. TODO(phase-4, stage 4): build_target() turns verified candidates into a Target.
"""

from __future__ import annotations

from cua.safety.redact import data_shape
from cua.schema.artifact import (
    CssLocator,
    LabelLocator,
    Locator,
    NearTextLocator,
    RoleLocator,
    TableCellLocator,
    TextLocator,
)
from cua.schema.trace import ElementSnapshot

_CONTROL_TAGS = {"input", "textarea", "select"}
# Roles the near_text engine can filter by (cua.surface.engines NEAR_TEXT_JS).
_NEAR_TEXT_ROLES = {"textbox", "combobox", "listbox", "checkbox", "radio", "spinbutton", "button"}


def is_ui_text(text: str | None) -> bool:
    """A visible label safe to anchor on: present, not a redaction token, not a data value."""
    return bool(text) and "«" not in (text or "") and data_shape(text or "") is None


def _row_anchor(snap: ElementSnapshot) -> str | None:
    """A label cell in the element's row (e.g. 'Share Savings'), never the element itself or data."""
    ctx = snap.table_context
    if ctx is None:
        return None
    own = snap.text or snap.accessible_name
    for cell in ctx.row_text.split(" | "):
        if is_ui_text(cell) and cell != own:
            return cell
    return None


def candidates(snap: ElementSnapshot) -> list[Locator]:
    out: list[Locator] = []
    is_control = snap.tag in _CONTROL_TAGS
    if snap.role and is_ui_text(snap.accessible_name):
        out.append(RoleLocator(role=snap.role, name=snap.accessible_name, exact=True))
    if is_ui_text(snap.label):
        out.append(LabelLocator(label=snap.label or ""))
    left = snap.nearby_text.get("left")
    if is_control and is_ui_text(left):
        out.append(LabelLocator(label=left or ""))  # legacy caption used as a label; often unverified
    if not is_control and is_ui_text(snap.text):
        out.append(TextLocator(text=snap.text or "", exact=True))
    ctx = snap.table_context
    anchor = _row_anchor(snap)
    if ctx and anchor and ctx.column_header and ctx.table_headers:
        out.append(
            TableCellLocator(
                table_has_header=ctx.table_headers[0], row_contains=anchor, column_header=ctx.column_header
            )
        )
    if is_control and is_ui_text(left):
        role = snap.role if snap.role in _NEAR_TEXT_ROLES else None
        out.append(NearTextLocator(anchor_text=left or "", relation="following_input", role=role))
    if snap.css_path:
        out.append(CssLocator(selector=snap.css_path))
    unique: list[Locator] = []
    for loc in out:
        if loc not in unique:
            unique.append(loc)
    return unique
