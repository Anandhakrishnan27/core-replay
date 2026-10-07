"""Build the LLM observation: a11y snapshot with element refs (+ optional masked screenshot).

The work happens in Surface (only Surface touches the browser): PlaywrightWebSurface.observe() takes one
ai-mode aria snapshot covering every frame (refs like f3e26), captures an ElementSnapshot per useful ref
(role, name, label, nearby text, table context, css path, frame path) and returns the tree already
redacted: typed values reduced to their length, tenant-masked text as «masked», data values as their
shape (e.g. «shape:currency»). The snapshots are kept so the recorder can attach the exact element to
every action. See cua.surface.aria.
"""

from __future__ import annotations

from cua.surface.base import Observation, Surface


async def observe(surface: Surface, with_screenshot: bool = False) -> Observation:
    return await surface.observe(with_screenshot=with_screenshot)
