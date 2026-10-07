"""Replay handoff end to end: escalate → operator takes control (real API) → human fixes the LIVE page →
hand back → resync to the furthest satisfied checkpoint → continue. Plus re-ask, abort, timeout, budget,
retry-before-action and "never past an irreversible step a human performed".

The scripted operator talks to the in-loop operator API over HTTP and plays the human by driving the
run's own page (the same browser context automation uses: invariant 10).
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from playwright.async_api import Browser, Frame

import cua.replay.executor as executor
from cua.handoff.operator import OperatorServer, operator_server
from cua.replay.executor import execute
from cua.schema.artifact import CapabilityArtifact
from cua.schema.result import RunResult, RunStatus
from cua.surface.wait import poll_until
from tests.test_replay_mockbank import assert_no_raw_pii, events

Human = Callable[["Operator"], Awaitable[None]]


@pytest.fixture(autouse=True)
def quick_resync(monkeypatch):
    monkeypatch.setattr(executor, "_RESYNC_WAIT_MS", 1_500)  # a failed resync waits this long


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Operator:
    """A person at the operator page: the HTTP API for control, the run's live page for fixing."""

    def __init__(self, op: OperatorServer, http: httpx2.AsyncClient, browser: Browser) -> None:
        self.op, self.http, self.browser = op, http, browser
        self.epoch = -1
        self.run: dict[str, Any] = {}

    async def paused(self, timeout_ms: int = 20_000) -> dict[str, Any]:
        """Wait for a NEW intervention request (PAUSED with a newer epoch than the last one seen)."""

        async def waiting() -> bool:
            runs = (await self.http.get("/api/interventions")).json()
            for r in runs:
                if r["state"] == "PAUSED" and r["epoch"] > self.epoch:
                    self.run, self.epoch = r, r["epoch"]
                    return True
            return False

        assert await poll_until(waiting, timeout_ms, 50), "no intervention request"
        return self.run

    async def post(self, op: str, **params: str) -> dict[str, Any]:
        r = await self.http.post(f"/api/runs/{self.run['run_id']}/{op}", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def take(self) -> None:
        await self.post("take-control", operator_id="ana@desk-1")

    async def hand_back(self) -> None:
        await self.post("hand-back")

    def main(self) -> Frame:
        [context] = self.browser.contexts  # the run's own (only) context
        frame = context.pages[0].frame(name="main")
        assert frame is not None
        return frame


@pytest.fixture
def handoff_run(approved_artifact, tenant, test_policy, browser, tmp_path):
    async def go(
        human: Human,
        member_id: str = "10001",
        fault: str | None = "maint",
        **kw: Any,
    ) -> tuple[RunResult, Operator]:
        async with operator_server(port=_free_port()) as op:
            async with httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{op.port}", headers={"X-Operator-Token": op.token}
            ) as http:
                person = Operator(op, http, browser)
                acting = asyncio.create_task(human(person))
                result = await execute(
                    kw.pop("artifact", approved_artifact),
                    tenant,
                    test_policy,
                    {"member_id": member_id},
                    fault=fault,
                    browser=browser,
                    evidence_root=tmp_path,
                    operator=op,
                    handoff_timeout_s=kw.pop("handoff_timeout_s", 20),
                    **kw,
                )
                await asyncio.wait_for(acting, 5)
                assert op.runs == {}, "the run unregisters when it ends"
                return result, person

    return go


def event_names(result: RunResult) -> list[str]:
    return [e["event"] for e in events(result)]


async def click_try_again(person: Operator) -> None:
    await person.main().get_by_text("Try Again").click()


# ---- the maint handoff: the demo path ---------------------------------------------------------- #


async def test_human_fixes_the_page_and_replay_resumes_after_the_checkpoint(handoff_run):
    async def human(person: Operator) -> None:
        run = await person.paused()
        assert run["request"]["step_id"] == "submit_search"
        assert run["request"]["category"] == "UNKNOWN_STATE"
        shot = await person.http.get(f"/api/runs/{run['run_id']}/screenshot")
        assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
        await person.take()
        await click_try_again(person)  # in the SAME live session automation was using
        await person.hand_back()

    result, _ = await handoff_run(human, member_id="10002")
    assert result.status is RunStatus.success, result.failure
    assert result.outputs["savings_balance"] == "1203.55"
    [h] = result.handoffs
    assert h.resolution == "resumed" and h.step_id == "submit_search"
    assert h.resumed_at_step == "read_balance"  # the submit step's checkpoint held: not repeated
    assert h.operator_id == "ana@desk-1"
    assert h.taken_at is not None and h.returned_at is not None
    assert h.requested_at <= h.taken_at <= h.returned_at
    assert any(a.kind == "click" and a.target_hint == "link 'Try Again'" for a in h.human_actions)

    names = event_names(result)
    assert names.index("escalation_requested") < names.index("resync") < names.index("handoff_resolved")
    assert names.count("human_action") >= 1
    resync = next(e for e in events(result) if e["event"] == "resync")
    assert resync["details"] == {"checkpoint": "submit_search", "resume_at": "read_balance"}
    assert_no_raw_pii(Path(result.evidence_dir))  # human actions and resync evidence included
    stored = json.loads((Path(result.evidence_dir) / "result.json").read_text())
    assert stored["handoffs"][0]["resolution"] == "resumed"


async def raw_click_try_again(person: Operator) -> None:
    """A trackpad-style click: trusted input at the link's coordinates, no actionability checks."""
    page = person.main().page
    box = await person.main().get_by_text("Try Again").bounding_box()
    assert box is not None
    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


async def test_stray_click_before_take_control_is_ignored_and_the_run_really_hands_off(handoff_run):
    async def human(person: Operator) -> None:
        await person.paused()
        await raw_click_try_again(person)  # before taking control: the input lock swallows it

        async def left_maintenance() -> bool:
            return "Maintenance Window" not in await person.main().locator("td.hdr").inner_text()

        assert not await poll_until(left_maintenance, 1_000, 50), "a stray click changed the live page"
        await person.take()
        await raw_click_try_again(person)  # the same click, now as the human in control
        await person.hand_back()

    result, _ = await handoff_run(human)
    assert result.status is RunStatus.success, result.failure
    [h] = result.handoffs
    assert h.resolution == "resumed" and h.resumed_at_step == "read_balance"
    clicks = [a for a in h.human_actions if a.kind == "click"]
    assert [a.target_hint for a in clicks] == ["link 'Try Again'"]  # only the click made in HUMAN


async def test_reauthentication_passes_the_input_lock(handoff_run):
    async def nobody(person: Operator) -> None:
        return None  # an operator is attached (so the lock is on), but nothing escalates

    result, _ = await handoff_run(nobody, fault="session_expired")
    assert result.status is RunStatus.success, result.failure
    assert [(r.condition_id, r.action, r.succeeded) for r in result.recoveries] == [
        ("session_expired", "reauthenticate", True)
    ]
    assert result.handoffs == []


async def test_hand_back_without_a_fix_asks_again_with_the_expected_state(handoff_run):
    async def human(person: Operator) -> None:
        await person.paused()
        await person.take()
        await person.hand_back()  # nothing fixed
        again = await person.paused()  # RESUMING → PAUSED: asked again, same live session
        assert "submit_search" in again["request"]["expected_state"]
        assert "member_header" in again["request"]["expected_state"]
        assert again["request"]["reason"].startswith("after hand-back the screen matches no checkpoint")
        await person.take()
        await click_try_again(person)
        await person.hand_back()

    result, _ = await handoff_run(human)
    assert result.status is RunStatus.success, result.failure
    [h] = result.handoffs  # one intervention, two hand-backs
    assert h.resolution == "resumed" and h.resumed_at_step == "read_balance"
    assert h.reason == "screen matches neither the checkpoint nor any known condition"  # the original
    assert event_names(result).count("resync_failed") == 1


async def test_hand_backs_are_bounded(handoff_run):
    async def human(person: Operator) -> None:
        for _ in range(executor._MAX_RESYNCS):
            await person.paused()
            await person.take()
            await person.hand_back()  # never fixed

    result, _ = await handoff_run(human)
    assert result.status is RunStatus.failed
    assert result.failure is not None and result.failure.category.value == "UNKNOWN_STATE"
    [h] = result.handoffs
    assert h.resolution == "aborted"
    assert event_names(result).count("resync") == executor._MAX_RESYNCS


async def test_operator_abort_fails_the_run(handoff_run):
    async def human(person: Operator) -> None:
        await person.paused()
        await person.take()
        await person.post("abort")

    result, _ = await handoff_run(human)
    assert result.status is RunStatus.failed
    assert result.failure is not None and result.failure.category.value == "UNKNOWN_STATE"
    assert result.failure.step_id == "submit_search"
    [h] = result.handoffs
    assert h.resolution == "aborted" and h.operator_id == "ana@desk-1" and h.returned_at is None


async def test_nobody_takes_control_times_out(handoff_run):
    async def nobody(person: Operator) -> None:
        await person.paused()

    result, _ = await handoff_run(nobody, handoff_timeout_s=0.5)
    assert result.status is RunStatus.failed
    [h] = result.handoffs
    assert h.resolution == "timed_out" and h.operator_id is None and h.taken_at is None


# ---- escalation BEFORE the action ran (target not found) --------------------------------------- #


def broken_search_button(raw: dict, *, irreversible: bool = False) -> CapabilityArtifact:
    """The search button's only locator no longer matches: TARGET_NOT_FOUND before the click."""
    raw["targets"]["search_button"]["locators"] = [{"strategy": "role", "role": "button", "name": "Find"}]
    step = next(s for s in raw["steps"] if s["id"] == "submit_search")
    step["timeout_ms"] = 1_000
    if irreversible:
        step["risk"] = "irreversible"
        raw["policy"] = {"max_risk": "irreversible", "requires_confirmation": True}
    return CapabilityArtifact.model_validate(raw)


async def click_search(person: Operator) -> None:
    await person.main().locator("form[name='lookup'] input[type='submit']").click()


async def test_human_performs_the_step_and_replay_resumes_after_it(handoff_run, raw_artifact):
    async def human(person: Operator) -> None:
        run = await person.paused()
        assert run["request"]["category"] == "TARGET_NOT_FOUND"
        await person.take()
        await click_search(person)  # the field was already filled by automation
        await person.hand_back()

    result, _ = await handoff_run(
        human, fault=None, artifact=broken_search_button(raw_artifact), mode="supervised"
    )
    assert result.status is RunStatus.success, result.failure
    [h] = result.handoffs
    assert h.resolution == "resumed" and h.resumed_at_step == "read_balance"
    assert any(a.target_hint == "button 'Search'" for a in h.human_actions)


async def test_step_that_never_ran_is_retried_after_hand_back(handoff_run, raw_artifact):
    async def human(person: Operator) -> None:
        await person.paused()
        await person.take()
        await person.hand_back()  # nothing done: no checkpoint holds, the click never ran → retry it
        await person.paused()  # the retry hits the same missing target: a NEW intervention
        await person.post("abort")

    result, _ = await handoff_run(
        human, fault=None, artifact=broken_search_button(raw_artifact), mode="supervised"
    )
    assert result.status is RunStatus.failed
    assert result.failure is not None and result.failure.category.value == "TARGET_NOT_FOUND"
    assert [h.resolution for h in result.handoffs] == ["resumed", "aborted"]
    assert result.handoffs[0].resumed_at_step == "submit_search"
    assert result.handoffs[0].intervention_id != result.handoffs[1].intervention_id


async def test_never_resumes_past_an_irreversible_step_a_human_performed(handoff_run, raw_artifact):
    async def human(person: Operator) -> None:
        await person.paused()
        await person.take()
        await click_search(person)  # the human commits the "irreversible" step
        await person.hand_back()

    result, _ = await handoff_run(
        human,
        fault=None,
        artifact=broken_search_button(raw_artifact, irreversible=True),
        mode="supervised",
        confirmed=True,
    )
    assert result.status is RunStatus.failed
    assert result.failure is not None and result.failure.category.value == "POLICY_VIOLATION"
    assert result.outputs == {}
    [h] = result.handoffs
    assert h.resolution == "completed_by_human" and h.resumed_at_step is None
    assert "read_balance" not in [e.get("step_id") for e in events(result) if e["event"] == "step_started"]
