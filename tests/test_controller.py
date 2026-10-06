import asyncio
from datetime import UTC, datetime

import pytest

from cua.handoff.controller import (
    ControlState,
    HandoffAborted,
    InvalidTransition,
    NotInControl,
    SessionControl,
)
from cua.handoff.models import InterventionRequest


def _req() -> InterventionRequest:
    return InterventionRequest(
        intervention_id="int_1",
        run_id="r1",
        mode="replay",
        reason="UNKNOWN_STATE",
        current_url="http://localhost:8000/console",
        requested_at=datetime.now(UTC),
    )


async def test_full_handoff_cycle():
    c = SessionControl("r1")
    c.ensure_automation()
    waiter = asyncio.create_task(c.request_intervention(_req(), timeout_s=5))
    await asyncio.sleep(0)
    assert c.state is ControlState.PAUSED
    with pytest.raises(NotInControl):
        c.ensure_automation()  # automation cannot act while paused
    c.take_control("op_demo")
    assert c.state is ControlState.HUMAN and c.holder == "human:op_demo"
    c.hand_back()
    await waiter
    assert c.state is ControlState.RESUMING
    c.resumed()
    c.ensure_automation()
    assert c.epoch == 4


async def test_abort():
    c = SessionControl("r1")
    waiter = asyncio.create_task(c.request_intervention(_req(), timeout_s=5))
    await asyncio.sleep(0)
    c.take_control("op")
    c.abort()
    with pytest.raises(HandoffAborted):
        await waiter
    assert c.state is ControlState.ABORTED


async def test_timeout_aborts():
    c = SessionControl("r1")
    with pytest.raises(HandoffAborted):
        await c.request_intervention(_req(), timeout_s=0.05)
    assert c.state is ControlState.ABORTED


def test_invalid_transitions():
    c = SessionControl("r1")
    with pytest.raises(InvalidTransition):
        c.take_control("op")  # nothing to take: automation holds control
    with pytest.raises(InvalidTransition):
        c.hand_back()
