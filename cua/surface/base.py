"""Surface: THE seam between "how we perceive/act on an app" and "the recorded flow".

Everything (discovery LLM, replay, handoff resync) touches the app only through this protocol.
Policy checks, redaction and evidence logging happen inside implementations, so nothing can bypass them.

  PlaywrightWebSurface  web + legacy web (frames, tables)           [implemented in this repo]
  DesktopSurface        Windows UIA / macOS AX, same 6 methods      [design only]
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from cua.schema.artifact import Action, Locator, Predicate, RiskClass, Target
from cua.schema.trace import ElementSnapshot, PageState


@dataclass
class Observation:
    """What the LLM sees during discovery."""

    page: PageState
    aria_snapshot: str  # a11y tree text with element refs (e1, e2, ...)
    refs: dict[str, ElementSnapshot] = field(default_factory=dict)
    screenshot_png: bytes | None = None  # optional, masked


@dataclass
class Resolved:
    """A target resolved to exactly one live element."""

    target_id: str
    locator_index: int  # which ranked locator won (0 = preferred). >0 is a drift signal
    handle: Any  # implementation-specific (Playwright Locator)


class Surface(Protocol):
    async def observe(self, with_screenshot: bool = False) -> Observation: ...

    async def resolve(self, target_id: str, target: Target) -> Resolved:
        """Try locators in order; first that matches EXACTLY ONE element and passes MatchRule wins.

        Raises TargetNotFound / TargetAmbiguous.
        """
        ...

    async def act(
        self, action: Action, resolved: Resolved | None, risk: RiskClass, *, value: str | None
    ) -> None:
        """Policy-gated action. `value` is the already-resolved template value (never logged raw)."""
        ...

    async def check(self, predicate: Predicate, targets: dict[str, Target]) -> bool: ...

    async def read(self, resolved: Resolved) -> str: ...

    async def snapshot(self, reason: str, *, dom: bool = False) -> list[str]:
        """Masked screenshot (+ DOM on failure). Returns evidence-relative paths."""
        ...

    # Discovery only: the LLM names elements by observation ref, not by artifact target.

    async def resolve_ref(self, ref: str) -> Resolved:
        """The live element behind a ref from the latest observe(). Raises TargetNotFound."""
        ...

    async def verify_locators(
        self, resolved: Resolved, frame_path: list[str], locators: Sequence[Locator]
    ) -> list[bool]:
        """Per locator: matches exactly one element now, and it is `resolved`."""
        ...

    async def settle(self, timeout_ms: int) -> bool:
        """Bounded wait until the page stops loading/changing after an action."""
        ...

    # Discovery handoff: describe what a human acted on (the element comes from the recorder).

    async def describe(self, handle: Any, role: str | None, name: str | None) -> ElementSnapshot | None:
        """Redacted ElementSnapshot of a live element; None if it is gone."""
        ...

    async def page_state(self) -> PageState:
        """The current page, redacted (UI text only)."""
        ...

    async def typed_value_index(self, handle: Any, values: Sequence[str]) -> int | None:
        """Index of the value a field holds among `values`, compared inside the app; None if none."""
        ...


class TargetNotFound(Exception): ...


class TargetAmbiguous(Exception): ...


class ActionFailed(Exception):
    """The app refused or timed out on an action. Message never contains the typed value."""
