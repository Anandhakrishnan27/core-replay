"""PlaywrightWebSurface: Surface implementation for web and legacy web apps. Phase 2.

Responsibilities:
  - one isolated BrowserContext per run (headed when handoff may be needed)
  - context.route("**/*") → Policy.origin_allowed (network-level allowlist, all frames)
  - Policy.authorize() before every act()
  - descend frame_path; resolve ranked locators + MatchRule; Playwright auto-waits for actionability
  - aria snapshot + element refs for discovery observations
  - masked screenshots (targets with sensitive=true), DOM dump on failure, Playwright tracing
"""

from __future__ import annotations

from typing import Any

from cua.config import Policy
from cua.evidence.logger import RunLogger
from cua.safety.policy import PolicyGate


class PlaywrightWebSurface:
    def __init__(self, page: Any, policy: Policy, gate: PolicyGate, logger: RunLogger) -> None:
        self.page = page
        self.policy = policy
        self.gate = gate
        self.logger = logger

    async def install_network_gate(self) -> None:
        """TODO(phase-2): await self.page.context.route("**/*", handler) using gate.origin_allowed."""
        raise NotImplementedError

    # observe / resolve / act / check / read / snapshot: TODO(phase-2), see cua.surface.base.Surface
