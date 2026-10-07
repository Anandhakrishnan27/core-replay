"""The operator input lock on the live mock bank: human input reaches the page only in HUMAN.

"Human" clicks are raw `page.mouse` clicks at an element's coordinates: trusted input with no actionability
checks, exactly what a trackpad produces (and what Playwright's own clicks look like to the page too).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import urlsplit

from playwright.async_api import Frame, Locator

from cua.handoff.controller import ControlState, SessionControl
from cua.handoff.lock import InputLock
from cua.handoff.models import InterventionRequest
from cua.surface.wait import poll_until
from tests.test_surface_mockbank import act_step, console  # noqa: F401  (console is a fixture)


def frame(session, name: str) -> Frame:
    f = session.page.frame(name=name)
    assert f is not None
    return f


def main_path(session) -> str:
    return urlsplit(frame(session, "main").url).path


async def raw_click(session, target: Locator) -> None:
    box = await target.bounding_box()
    assert box is not None
    await session.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


async def lands_on(session, path: str, timeout_ms: int = 3_000) -> bool:
    async def there() -> bool:
        return main_path(session) == path

    return await poll_until(there, timeout_ms, 50)


async def stays_on(session, path: str) -> bool:
    """Still on `path` after giving a navigation every chance to start (bounded)."""
    return not await lands_on(session, "__elsewhere__", 1_000) and main_path(session) == path


def request() -> InterventionRequest:
    return InterventionRequest(
        intervention_id="i1",
        run_id="r1",
        mode="replay",
        reason="test",
        current_url="http://localhost/",
        requested_at=datetime.now(UTC),
    )


async def test_human_input_reaches_the_page_only_while_a_human_holds_control(
    console,  # noqa: F811
    session,
    make_surface,
    artifact,
):
    control = SessionControl("r1")
    lock = InputLock(session.context, control)
    await lock.install()
    surface = make_surface(control=control, input_lock=lock)
    nav = frame(session, "nav")
    lookup, home = nav.get_by_role("link", name="Member Lookup"), nav.get_by_role("link", name="Home")
    assert main_path(session) == "/console/home"

    # AUTOMATION: a stray human click does nothing; automation's own click goes through.
    await raw_click(session, lookup)
    assert await stays_on(session, "/console/home")
    await act_step(surface, artifact, "go_to_lookup")
    assert await lands_on(session, "/console/mbrlookup")
    # The document automation just opened starts locked as well.
    await raw_click(session, home)
    assert await stays_on(session, "/console/mbrlookup")

    # PAUSED (asking for a human): still locked until someone takes control.
    waiting = asyncio.create_task(control.request_intervention(request(), timeout_s=10))
    assert await poll_until(lambda: _is(control, ControlState.PAUSED), 2_000, 20)
    await raw_click(session, home)
    assert await stays_on(session, "/console/mbrlookup")

    # HUMAN: the person's clicks work, also in documents opened meanwhile.
    control.take_control("ana")
    await control.flush()
    await raw_click(session, home)
    assert await lands_on(session, "/console/home")
    await raw_click(session, lookup)
    assert await lands_on(session, "/console/mbrlookup")

    # Hand back: locked again before automation resumes.
    control.hand_back()
    await waiting
    await control.flush()
    control.resumed()
    await control.flush()
    await raw_click(session, home)
    assert await stays_on(session, "/console/mbrlookup")


async def _is(control: SessionControl, state: ControlState) -> bool:
    return control.state is state
