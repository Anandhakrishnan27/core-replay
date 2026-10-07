"""Compiler pass: clean. The shortest effective path through the trace.

- failed actions are dropped (they did nothing the replay should repeat)
- `dismiss` actions leave the path and become interstitials (→ recoverable conditions); the action
  before a dismiss takes the dismiss's page_after, so its checkpoint describes the real next screen
- the last fill per element and the last extract per output win
- navigation loops are cut: a run of clicks/presses/selects that ends on the screen it started from
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.schema.trace import DiscoveryTrace, ElementSnapshot, PageState, TraceAction

_NAV_TOOLS = {"click", "press", "select"}


@dataclass
class Cleaned:
    actions: list[TraceAction]  # the replayable path, in order
    interstitials: list[TraceAction]  # dismissed dialogs (page_before shows the dialog)


def element_key(el: ElementSnapshot | None) -> tuple[object, ...] | None:
    if el is None:
        return None
    return (tuple(el.frame_path), el.tag, el.css_path, el.xpath)


def _state(page: PageState) -> tuple[str, tuple[str, ...]]:
    return page.url, tuple(page.headings)


def clean(trace: DiscoveryTrace) -> Cleaned:
    path: list[TraceAction] = []
    interstitials: list[TraceAction] = []
    for a in trace.actions:
        if not a.ok:
            continue
        if a.tool == "dismiss":
            interstitials.append(a)
            if path:
                path[-1] = path[-1].model_copy(update={"page_after": a.page_after})
            continue
        path.append(a)
    return Cleaned(actions=_cut_loops(_last_wins(path)), interstitials=interstitials)


def _last_wins(path: list[TraceAction]) -> list[TraceAction]:
    keep: list[TraceAction] = []
    for i, a in enumerate(path):
        later = path[i + 1 :]
        if a.tool == "fill" and any(
            b.tool == "fill" and element_key(b.element) == element_key(a.element) for b in later
        ):
            continue
        if a.tool == "extract" and any(b.tool == "extract" and b.output_name == a.output_name for b in later):
            continue
        keep.append(a)
    return keep


def _cut_loops(path: list[TraceAction]) -> list[TraceAction]:
    """Remove the longest span of navigation-only actions that returns to the screen it left."""
    changed = True
    while changed:
        changed = False
        for j in range(len(path)):
            for i in range(j + 1):  # earliest start first: the longest loop ending at j
                span = path[i : j + 1]
                if (
                    _state(path[j].page_after) == _state(path[i].page_before)
                    and all(a.tool in _NAV_TOOLS for a in span)
                    and any(_state(a.page_after) != _state(a.page_before) for a in span)
                ):
                    path = path[:i] + path[j + 1 :]
                    changed = True
                    break
            if changed:
                break
    return path
