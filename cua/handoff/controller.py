"""Control-transfer model for ONE live browser session.

States:
    AUTOMATION ──request_intervention()──► PAUSED ──take_control(op)──► HUMAN
    HUMAN ──hand_back()──► RESUMING ──resumed()──► AUTOMATION          (resync found a satisfied checkpoint)
    RESUMING ──request_intervention()──► PAUSED                         (resync failed: ask the human again)
    PAUSED | HUMAN ──abort() / timeout──► ABORTED

Invariants:
  - Automation calls ensure_automation() before EVERY action; it raises unless state is AUTOMATION.
  - `epoch` increments on every transfer, so stale actions from an earlier holder are detectable.
  - The browser context is never recreated: the human uses the same live session.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import Enum

from cua.handoff.models import InterventionRequest


class ControlState(str, Enum):
    AUTOMATION = "AUTOMATION"
    PAUSED = "PAUSED"
    HUMAN = "HUMAN"
    RESUMING = "RESUMING"
    ABORTED = "ABORTED"


class NotInControl(Exception):
    pass


class InvalidTransition(Exception):
    pass


class HandoffAborted(Exception):
    pass


class SessionControl:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state = ControlState.AUTOMATION
        self.holder: str = "automation"
        self.epoch = 0
        self.request: InterventionRequest | None = None
        self.history: list[tuple[datetime, ControlState, str]] = []
        self._released = asyncio.Event()

    # -- helpers -------------------------------------------------------------
    def _move(self, allowed_from: set[ControlState], to: ControlState, holder: str) -> None:
        if self.state not in allowed_from:
            raise InvalidTransition(f"{self.state.value} → {to.value} not allowed")
        self.state, self.holder = to, holder
        self.epoch += 1
        self.history.append((datetime.now(UTC), to, holder))

    # -- automation side ------------------------------------------------------
    def ensure_automation(self) -> None:
        if self.state is not ControlState.AUTOMATION:
            raise NotInControl(f"automation may not act while state is {self.state.value} ({self.holder})")

    async def request_intervention(self, request: InterventionRequest, timeout_s: float) -> None:
        """Pause automation and wait (same session stays open) until a human hands back or aborts.

        Also called from RESUMING when resync fails, with `expected_state` telling the operator what
        the automation needs to see before it can continue.
        """
        self._move({ControlState.AUTOMATION, ControlState.RESUMING}, ControlState.PAUSED, "none")
        self.request = request
        self._released.clear()
        try:
            await asyncio.wait_for(self._released.wait(), timeout=timeout_s)
        except TimeoutError:
            self._move({ControlState.PAUSED, ControlState.HUMAN}, ControlState.ABORTED, "none")
            raise HandoffAborted("handoff timed out") from None
        if self.state is ControlState.ABORTED:
            raise HandoffAborted("operator aborted")
        # state is RESUMING: caller resyncs to the furthest satisfied checkpoint, then calls resumed()

    def resumed(self) -> None:
        self._move({ControlState.RESUMING}, ControlState.AUTOMATION, "automation")
        self.request = None

    # -- operator side --------------------------------------------------------
    def take_control(self, operator_id: str) -> None:
        self._move({ControlState.PAUSED}, ControlState.HUMAN, f"human:{operator_id}")

    def hand_back(self) -> None:
        self._move({ControlState.HUMAN}, ControlState.RESUMING, "automation")
        self._released.set()

    def abort(self) -> None:
        self._move({ControlState.PAUSED, ControlState.HUMAN}, ControlState.ABORTED, "none")
        self._released.set()
