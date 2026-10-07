"""Compiler pass: locators. Candidates from an ElementSnapshot, in rank order.

    role → label → text → table_cell → near_text → css

`candidates()` is pure and runs at ACTION time during discovery: Surface.verify_locators() then keeps only
the ones that match exactly the acted element on the live page (ElementSnapshot.verified_locators).
Nothing is built from data: a name or text that is a redaction token («shape:…», «masked») never
becomes a locator.

`build_target()` (compile time) keeps the verified candidates in rank order, minus POSITIONAL css/xpath
(`:nth-of-type`, `[n]` steps): in a data table the row position changes with the data, so a positional
fallback would silently read another row's value. A target needs at least one semantic locator.
"""

from __future__ import annotations

import re

from cua.compiler import CompileError
from cua.safety.redact import data_shape
from cua.schema.artifact import (
    SEMANTIC_STRATEGIES,
    CssLocator,
    LabelLocator,
    Locator,
    MatchRule,
    NearTextLocator,
    RoleLocator,
    TableCellLocator,
    Target,
    TextLocator,
    XPathLocator,
)
from cua.schema.trace import ElementSnapshot

_CONTROL_TAGS = {"input", "textarea", "select"}
# Roles the near_text engine can filter by (cua.surface.engines NEAR_TEXT_JS).
_NEAR_TEXT_ROLES = {"textbox", "combobox", "listbox", "checkbox", "radio", "spinbutton", "button"}


def is_ui_text(text: str | None) -> bool:
    """A visible label safe to anchor on: present, not a redaction token, not a data value."""
    return bool(text) and "«" not in (text or "") and data_shape(text or "") is None


def row_anchor(snap: ElementSnapshot) -> str | None:
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
    anchor = row_anchor(snap)
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


# ---- build_target (compile time) ---------------------------------------------------------------- #

_POSITIONAL_XPATH_RE = re.compile(r"\[\d+\]")
_CLICKABLE_TAGS = _CONTROL_TAGS | {"button"}
TEXT_PATTERNS = {
    "currency": r"^-?[$€£]?-?[0-9,]+\.[0-9]{2}$",
    "decimal": r"^-?[0-9,]*\.[0-9]+$",
    "integer": r"^-?[0-9,]+$",
    "date": r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$",
}
_WHY = {
    "role": "accessible role + name (what an operator sees)",
    "label": "its associated label",
    "text": "its visible text",
    "table_cell": "row label + column header (survives row and column reordering)",
    "near_text": "the visible caption beside it (legacy form without <label for>)",
    "css": "stable markup attributes (fallback)",
    "xpath": "markup path (fallback)",
}


def is_positional(loc: Locator) -> bool:
    if isinstance(loc, CssLocator):
        return ":nth-of-type(" in loc.selector or ":nth-child(" in loc.selector
    if isinstance(loc, XPathLocator):
        return bool(_POSITIONAL_XPATH_RE.search(loc.xpath))
    return False


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "element"


def describe(el: ElementSnapshot) -> tuple[str, str]:
    """(id base, human description) for a target."""
    ctx, anchor = el.table_context, row_anchor(el)
    if ctx and anchor:
        return (
            slug(f"{anchor}_{ctx.column_header}_cell"),
            f"'{ctx.column_header}' cell in the '{anchor}' row of the table headed '{ctx.table_headers[0]}'",
        )
    name = el.accessible_name if is_ui_text(el.accessible_name) else None
    if el.tag in _CONTROL_TAGS and el.role not in ("button",):
        cap = next((t for t in (el.label, el.nearby_text.get("left"), name) if is_ui_text(t)), None)
        return (slug(f"{cap}_field") if cap else "input_field"), (
            f"input field captioned '{cap}'" if cap else "input field"
        )
    if name and el.role:
        return slug(f"{name}_{el.role}"), f"{el.role} '{name}'"
    text = el.text if is_ui_text(el.text) else None
    return slug(text or el.tag), (f"'{text}' {el.tag}" if text else f"{el.tag} element")


def build_target(
    el: ElementSnapshot,
    *,
    sensitive: bool = False,
    text_pattern: str | None = None,
    frame_path: list[str] | None = None,
    where: str = "",
) -> Target:
    kept = [loc for loc in el.verified_locators if not is_positional(loc)]
    dropped = len(el.verified_locators) - len(kept)
    if not any(loc.strategy in SEMANTIC_STRATEGIES for loc in kept):
        raise CompileError(
            f"{where or 'target'}: no semantic locator was verified unique on the live page "
            f"(verified: {[loc.strategy for loc in el.verified_locators] or 'none'})"
        )
    is_control = el.tag in _CLICKABLE_TAGS or el.role in (
        "button",
        "textbox",
        "combobox",
        "checkbox",
        "radio",
    )
    rationale = "Verified unique on the live page during discovery; tried in order: " + "; ".join(
        _WHY.get(loc.strategy, loc.strategy) for loc in kept
    )
    if dropped:
        rationale += f". Dropped {dropped} positional fallback(s): row position changes with the data."
    if text_pattern:
        rationale += " text_pattern guards against reading the wrong cell."
    return Target(
        description=describe(el)[1],
        locators=kept,
        frame_path=list(el.frame_path) if frame_path is None else frame_path,
        match=MatchRule(enabled=True if is_control else None, text_pattern=text_pattern),
        sensitive=sensitive,
        rationale=rationale,
    )
