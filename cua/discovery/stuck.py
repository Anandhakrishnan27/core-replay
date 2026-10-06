"""Stuck detection for discovery: escalate instead of wandering."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class StuckDetector:
    max_steps: int
    repeat_limit: int = 3
    steps: int = 0
    consecutive_failures: int = 0
    _last_obs: str | None = None
    _repeats: int = 0
    reasons: list[str] = field(default_factory=list)

    def record(self, observation_text: str, action_ok: bool) -> str | None:
        """Call once per loop iteration. Returns a stuck reason, or None."""
        self.steps += 1
        digest = hashlib.sha1(observation_text.encode()).hexdigest()
        self._repeats = self._repeats + 1 if digest == self._last_obs else 1
        self._last_obs = digest
        self.consecutive_failures = 0 if action_ok else self.consecutive_failures + 1

        if self.steps >= self.max_steps:
            return "step budget exhausted"
        if self._repeats >= self.repeat_limit:
            return f"same screen observed {self._repeats} times in a row"
        if self.consecutive_failures >= 3:
            return "3 consecutive actions failed"
        return None
