"""Compiler pass: checkpoints. page_before/page_after diffs → postconditions that hold for EVERY input.

Only UI chrome is ever asserted, never data:
  - a checkpoint text is a heading that APPEARED (in page_after, not in page_before's headings or UI text),
    with no digits at all (version strings and counts change) and no redaction token
  - target_visible() of the next step's target: the control or cell the flow needs next
Fill steps get no checkpoint (nothing navigates). The app fingerprint is the first stable heading on the
start screen plus the first step's target.
"""

from __future__ import annotations

from cua.compiler.locators import is_ui_text
from cua.schema.artifact import Check, Predicate, TargetVisible, TextPresent
from cua.schema.trace import PageState

FINGERPRINT_TIMEOUT_MS = 8_000


def stable_text(text: str | None) -> bool:
    return is_ui_text(text) and not any(c.isdigit() for c in text or "")


def new_heading(before: PageState, after: PageState) -> str | None:
    """The first heading that appeared with this step (e.g. 'Member Summary')."""
    seen = set(before.headings) | set(before.visible_texts)
    return next((h for h in after.headings if h not in seen and stable_text(h)), None)


def changed(before: PageState, after: PageState) -> bool:
    return (before.url, before.headings) != (after.url, after.headings)


def step_expect(heading: str | None, next_target: str | None) -> Check | None:
    preds: list[Predicate] = []
    if next_target:
        preds.append(TargetVisible(target=next_target))
    if heading:
        preds.append(TextPresent(text=heading))
    return Check(all_of=preds) if preds else None


def fingerprint(start: PageState, first_target: str | None) -> Check:
    preds: list[Predicate] = []
    heading = next((h for h in start.headings if stable_text(h)), None)
    if heading:
        preds.append(TextPresent(text=heading))
    if first_target:
        preds.append(TargetVisible(target=first_target))
    if not preds:
        preds.append(TextPresent(text=start.title))
    return Check(all_of=preds, timeout_ms=FINGERPRINT_TIMEOUT_MS)
