"""Human-action recorder on the live mock bank: only while HUMAN, typed values never leave the page.

The test plays the human by driving the page directly (that is what a person in the headed window does).
"""

from __future__ import annotations

import json

import pytest

from cua.handoff.controller import ControlState, SessionControl
from cua.handoff.recorder import HumanRecorder
from cua.session.provider import SessionProvider
from cua.surface.wait import poll_until
from mockbank.data import find_member
from tests.conftest import log_events
from tests.test_surface_mockbank import act_step, search, set_fault, step, wait_holds


@pytest.fixture
async def rec(session, tenant, make_surface, artifact, mockbank_url, run_logger):
    """Recorder installed BEFORE login (as a run does), console open, automation in control."""
    control = SessionControl("r1")
    seen: list[tuple[str, str | None]] = []

    async def on_action(action, element):
        seen.append((action.kind, await element.handle.evaluate("e => e.tagName") if element else None))

    recorder = HumanRecorder(
        session.context, control, run_logger, mask_selectors=tenant.mask_selectors, on_action=on_action
    )
    await recorder.install()
    await SessionProvider(tenant).login(session)
    driver = make_surface()
    await act_step(driver, artifact, "open_lookup", value=f"{mockbank_url}/console")
    return recorder, control, driver, seen


def take(control: SessionControl) -> None:
    control.state = ControlState.PAUSED  # as an escalation leaves it
    control.take_control("tester")


async def recorded(recorder: HumanRecorder, n: int) -> bool:
    async def enough() -> bool:
        return len(recorder.actions) >= n

    return await poll_until(enough, 3000, 50)


def main(session):
    frame = session.page.frame(name="main")
    assert frame is not None
    return frame


async def test_nothing_is_recorded_while_automation_acts(rec, artifact):
    recorder, _, driver, _ = rec
    await act_step(driver, artifact, "go_to_lookup")
    assert await wait_holds(driver, step(artifact, "go_to_lookup").expect, artifact)
    await act_step(driver, artifact, "enter_member_id", value="10002")
    assert recorder.actions == []


async def test_human_click_fill_and_navigation_are_recorded_redacted(rec, session, run_logger):
    recorder, control, _, seen = rec
    take(control)
    await session.page.frame(name="nav").get_by_text("Member Lookup").click()
    field = main(session).locator("input[name=mbrno]")
    await field.wait_for()
    await field.fill("10002")
    await main(session).locator("input[type=submit]").click()
    await main(session).get_by_text("Member Summary").wait_for()
    assert await recorded(recorder, 4)

    kinds = [a.kind for a in recorder.actions]
    assert kinds[:1] == ["click"] and "fill" in kinds and "navigate" in kinds
    fill = next(a for a in recorder.actions if a.kind == "fill")
    assert fill.value == "«redacted:len=5»" and fill.target_hint == "textbox 'Member Number'"
    assert any(a.kind == "click" and a.target_hint == "button 'Search'" for a in recorder.actions)
    assert ("click", "A") in seen and ("fill", "INPUT") in seen  # element handles reach the callback

    everything = json.dumps([a.model_dump(mode="json") for a in recorder.actions])
    everything += (run_logger.dir / "run.jsonl").read_text()
    assert "10002" not in everything
    assert [e["event"] for e in log_events(run_logger)].count("human_action") == len(recorder.actions)


async def test_masked_and_data_cells_never_show_their_text(rec, session, artifact):
    recorder, control, driver, _ = rec
    member = find_member("10002")
    assert member is not None
    await search(driver, artifact, member.member_id)
    assert await wait_holds(driver, step(artifact, "submit_search").expect, artifact)
    take(control)
    await main(session).locator("td.cap + td").nth(1).click()  # the member's name (tenant mask_selector)
    await main(session).locator("td.amt").nth(1).click()  # a balance
    assert await recorded(recorder, 2)
    hints = [a.target_hint for a in recorder.actions]
    assert hints == ["td '«masked»'", "td '«shape:currency»'"]
    assert member.name not in json.dumps(hints)


async def test_documents_open_before_install_listen_after_arm(session, tenant, run_logger, mockbank_url):
    await SessionProvider(tenant).login(session)  # pages load BEFORE the recorder exists
    control = SessionControl("r2")
    recorder = HumanRecorder(session.context, control, run_logger)
    await recorder.install()
    await recorder.arm()
    take(control)
    await session.page.frame(name="nav").get_by_text("Home").click()
    assert await recorded(recorder, 1)
    assert recorder.actions[0].target_hint == "link 'Home'"


async def test_password_fields_are_never_reported(rec, session, mockbank_url):
    recorder, control, _, _ = rec
    await set_fault(session, mockbank_url, "none")
    take(control)
    await session.page.goto(f"{mockbank_url}/login")  # the human opens the sign-on page
    await session.page.fill("form[name='signon'] input[name='password']", "hunter2-secret")
    await session.page.fill("form[name='signon'] input[name='userid']", "someone")  # password field blurs
    await session.page.press("form[name='signon'] input[name='userid']", "Tab")  # `change` fires on blur
    assert await recorded(recorder, 2)  # navigate + the user-id fill; the password field reported nothing
    assert all("password" not in a.target_hint for a in recorder.actions)
    assert not any(a.kind == "fill" and a.value == "«redacted:len=14»" for a in recorder.actions)
