"""Map schema locator strategies to Playwright locators (frame-aware).

role        → frame.get_by_role(role, name=..., exact=...)
label       → frame.get_by_label(label, exact=...)
text        → frame.get_by_text(text, exact=...)
table_cell  → custom engine (cua.surface.engines): table whose header row contains `table_has_header`;
              column index = position of `column_header`; row = tr containing `row_contains`
near_text   → custom engine: control following / right of / below the visible caption
css / xpath → frame.locator(...)
coordinates → last resort; element_at() via elementFromPoint (descends frames), then MatchRule verification
"""

from __future__ import annotations

import json
from typing import Any, cast

from playwright.async_api import ElementHandle, Frame, Page
from playwright.async_api import Locator as PWLocator

from cua.schema.artifact import Locator

_MAX_FRAME_DEPTH = 8


def to_playwright(frame: Frame, locator: Locator) -> PWLocator:
    """Return a lazy Playwright Locator for one strategy (all but `coordinates`)."""
    match locator.strategy:
        case "role":
            return frame.get_by_role(cast(Any, locator.role), name=locator.name, exact=locator.exact)
        case "label":
            return frame.get_by_label(locator.label, exact=locator.exact)
        case "text":
            return frame.get_by_text(locator.text, exact=locator.exact)
        case "table_cell" | "near_text":
            body = json.dumps(locator.model_dump(exclude={"strategy"}))
            return frame.locator(f"{locator.strategy}={body}")
        case "css":
            return frame.locator(locator.selector)
        case "xpath":
            return frame.locator(f"xpath={locator.xpath}")
    raise ValueError(f"strategy '{locator.strategy}' has no lazy locator; use element_at()")


def frames_for(page: Page, frame_path: list[str]) -> list[Frame]:
    """Frames to search. Empty path: the top document and every frame (caller requires exactly one match).

    Otherwise descend child frames by name, falling back to a URL fragment. A missing frame → [].
    Re-resolved on every call because navigation replaces frames; detached frames are skipped.
    """
    if not frame_path:
        return [f for f in page.frames if not f.is_detached()]
    current = page.main_frame
    for segment in frame_path:
        # after re-navigation, the replaced frame can still be listed briefly: skip detached ones
        children = [f for f in current.child_frames if not f.is_detached()]
        nxt = next((f for f in children if f.name == segment), None) or next(
            (f for f in children if segment in f.url), None
        )
        if nxt is None:
            return []
        current = nxt
    return [current]


async def element_at(page: Page, x: float, y: float) -> ElementHandle | None:
    """Element at normalized viewport coordinates, descending into frames. Ignores frame_path."""
    viewport = page.viewport_size
    if viewport is None:
        return None
    px, py = x * viewport["width"], y * viewport["height"]
    frame = page.main_frame
    for _ in range(_MAX_FRAME_DEPTH):
        js = await frame.evaluate_handle("([x, y]) => document.elementFromPoint(x, y)", [px, py])
        handle = js.as_element()
        if handle is None:
            return None
        child = await handle.content_frame()
        if child is None:
            return handle
        ox, oy = await handle.evaluate(
            "e => { const r = e.getBoundingClientRect();"
            " return [r.left + e.clientLeft, r.top + e.clientTop]; }"
        )
        px, py, frame = px - ox, py - oy, child
    return None
