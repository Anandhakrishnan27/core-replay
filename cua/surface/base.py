"""Surface: THE seam between "how we perceive/act on an app" and "the recorded flow".

Everything (discovery LLM, replay, handoff resync) touches the app only through this protocol.
Policy checks, redaction and evidence logging happen inside implementations, so nothing can bypass them.

  PlaywrightWebSurface  web + legacy web (frames, tables)           [implemented in this repo]
  DesktopSurface        Windows UIA / macOS AX, same 6 methods      [design only]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cua.schema.artifact import Action, Predicate, RiskClass, Target
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


class TargetNotFound(Exception): ...


class TargetAmbiguous(Exception): ...


class ActionFailed(Exception):
    """The app refused or timed out on an action. Message never contains the typed value."""
