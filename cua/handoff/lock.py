"""Input lock: the live browser accepts human input only while a human holds control.

The headed window the operator uses is the SAME window automation drives (invariant 10), so without a
lock a stray click while automation holds control (AUTOMATION, PAUSED, RESUMING) would change the
session behind the run's back, unrecorded. Playwright's own clicks reach the page as trusted events,
exactly like a trackpad's, so the page cannot tell them apart by `isTrusted`. Instead:

    every document   blocks pointer / mouse / key / drop / paste / submit input in the capture phase
                     on `window` (before the app and the human recorder see it), unless
    unlocked         SessionControl.state is HUMAN, or automation opened the lock for ONE action
                     (`async with lock.automation()`, e.g. a click, or a re-login that loads a new page)

Fail-closed: a new document starts locked and asks Python whether it is locked (`__cuaLocked`). A
blocked event is never a human action, so the recorder never sees it. Installed only when an operator
is attached (headed browser); headless runs have no window a person could click.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import BrowserContext, Frame
from playwright.async_api import Error as PlaywrightError

from cua.handoff.controller import ControlState, SessionControl

BINDING = "__cuaLockedNow"
_APPLY_JS = "(s) => { if (window.__cuaApplyLock) window.__cuaApplyLock(s); }"

LOCK_SCRIPT = r"""
(() => {
  if (window.__cuaLockInstalled) return; window.__cuaLockInstalled = true;
  window.__cuaLocked = true;  // fail closed until Python says who is in control
  window.__cuaLockVersion = -1;
  // [version, locked]: an answer older than one already applied is stale (e.g. it was computed while
  // automation's input window was open, and the window has closed since).
  window.__cuaApplyLock = ([version, locked]) => {
    if (version < window.__cuaLockVersion) return;
    window.__cuaLockVersion = version; window.__cuaLocked = locked !== false;
  };
  try {
    const p = window.__cuaLockedNow && window.__cuaLockedNow();
    if (p && p.then) p.then(window.__cuaApplyLock, () => {});
  } catch (_) {}
  const block = (e) => {
    if (!window.__cuaLocked) return;
    e.stopImmediatePropagation();
    if (e.cancelable) e.preventDefault();
  };
  for (const t of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'auxclick',
                   'contextmenu', 'keydown', 'keypress', 'keyup', 'dragstart', 'drop', 'paste', 'submit']) {
    window.addEventListener(t, block, true);
  }
})();
"""


class InputLock:
    def __init__(self, context: BrowserContext, control: SessionControl) -> None:
        self.context = context
        self.control = control
        self._automation_acting = False
        self._version = 0  # bumped on every change of `locked`; pages ignore older answers

    @property
    def locked(self) -> bool:
        return self.control.state is not ControlState.HUMAN and not self._automation_acting

    def _state(self) -> list[object]:
        return [self._version, self.locked]

    async def install(self) -> None:
        """Every new document gets the lock; documents already open get it now. Follows the control."""
        await self.context.expose_binding(BINDING, lambda _source: self._state())
        await self.context.add_init_script(LOCK_SCRIPT)
        await self._each_frame("() => " + LOCK_SCRIPT.strip().removesuffix(";"))
        await self.sync()
        self.control.subscribe(self._on_change)  # async: SessionControl.flush() waits for it

    async def sync(self) -> None:
        """Push the current lock state into every open document."""
        await self._each_frame(_APPLY_JS, self._state())

    @asynccontextmanager
    async def automation(self) -> AsyncIterator[None]:
        """Let automation's own input through for one action, then close again (also on error).

        Documents loaded meanwhile (a re-login navigates to the sign-on page) ask and are told unlocked.
        """
        self._automation_acting, self._version = True, self._version + 1
        await self.sync()
        try:
            yield
        finally:
            self._automation_acting, self._version = False, self._version + 1
            await self.sync()

    async def _on_change(self, _state: ControlState) -> None:
        self._version += 1
        await self.sync()

    async def _each_frame(self, script: str, arg: object = None) -> None:
        frames: list[Frame] = [f for page in self.context.pages for f in page.frames if not f.is_detached()]
        for frame in frames:
            try:
                await frame.evaluate(script, arg)
            except PlaywrightError:
                continue  # navigating: its new document runs the init script and starts locked
