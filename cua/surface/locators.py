"""Map schema locator strategies to Playwright locators (frame-aware). Phase 2.

role        → frame.get_by_role(role, name=..., exact=...)
label       → frame.get_by_label(label, exact=...)
text        → frame.get_by_text(text, exact=...)
table_cell  → find <table> whose header row contains `table_has_header`;
              column index = position of `column_header`; row = tr containing `row_contains`;
              return that row's cell at the column index
near_text   → element following the visible caption (same row / next cell / next input)
css / xpath → frame.locator(...)
coordinates → last resort; resolved via elementFromPoint + MatchRule verification
"""

from __future__ import annotations

from typing import Any

from cua.schema.artifact import Locator


def to_playwright(frame: Any, locator: Locator) -> Any:
    """Return a Playwright Locator for one strategy. TODO(phase-2)."""
    raise NotImplementedError("phase-2: locator strategy mapping")
