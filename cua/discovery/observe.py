"""Build the LLM observation: a11y snapshot with element refs (+ optional masked screenshot). Phase 4.

Approach: walk frames; use locator.aria_snapshot() per frame for the readable tree, and an injected
script to assign stable refs (e1..eN) to interactive/text elements, capturing an ElementSnapshot for
each ref (role, name, label, nearby text, table context, css path). The snapshots are kept so the
recorder can attach the exact element to every action.
"""

from __future__ import annotations

from cua.surface.base import Observation, Surface


async def observe(surface: Surface, with_screenshot: bool = False) -> Observation:
    return await surface.observe(with_screenshot=with_screenshot)
