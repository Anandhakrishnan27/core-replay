"""Capture what the human does in the live session, redacted. Same context, same pages as automation.

Mechanism: `context.expose_binding("__cuaRecord", handler)` + `context.add_init_script(LISTENER)`
on the run's own context (installed before login, so every document gets the listener), and `arm()` to
inject it into documents that were already open. The listener is passive (capture phase, never
preventDefault). It sends plain JSON; the element itself stays in an in-page map (`window.__cuaEls`)
and is fetched by id from the reporting frame only when a caller needs it (discovery). It reports:

    click   on the nearest interactive ancestor       → role + accessible name
    change  on text fields (fill) / selects (select)  → the typed value leaves the page only as its LENGTH;
                                                         select options are shape-redacted
    keydown Enter in a field                          → press
    navigation of any frame (Python side)             → frame name + path only
Password fields are never reported at all. Events arrive while automation acts too; the recorder keeps
them only while SessionControl.state is HUMAN or RESUMING (automation never acts while RESUMING, so a
late-delivered event from just before hand-back is still the human's). `drain()` after hand-back
waits for events the page already sent.

On the Python side every hint goes through the same redaction as discovery observations: tenant
`mask_selectors` text → «masked», data values → «shape:…», other long numbers hashed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, ElementHandle, Frame, Page
from playwright.async_api import Error as PlaywrightError

from cua.evidence.logger import RunLogger
from cua.handoff.controller import ControlState, SessionControl
from cua.safety.redact import page_value, redact_digit_runs, redact_typed
from cua.schema.result import HumanAction
from cua.surface.wait import poll_until

BINDING = "__cuaRecord"

# %MASK% is replaced by the tenant's mask_selectors (JSON) at install time.
_LISTENER_TEMPLATE = r"""
(() => {
  if (window.__cuaListening) return; window.__cuaListening = true;
  const MASK = %MASK%;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const masked = (e) => MASK.some((s) => { try { return !!e.closest(s); } catch (_) { return false; } });
  const INTERACTIVE = 'a, button, input, select, textarea, [role=button], [role=link], [role=checkbox], [role=tab], [role=menuitem]';
  const TEXT_TYPES = ['', 'text', 'search', 'email', 'tel', 'url', 'number'];
  const type = (e) => (e.getAttribute('type') || '').toLowerCase();
  const role = (e) => {
    if (e.getAttribute('role')) return e.getAttribute('role');
    const t = e.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || (t === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(type(e)))) return 'button';
    if (t === 'TEXTAREA' || (t === 'INPUT' && TEXT_TYPES.includes(type(e)))) return 'textbox';
    if (t === 'SELECT') return 'combobox';
    if (t === 'INPUT') return type(e);
    return t.toLowerCase();
  };
  const caption = (e) => {
    const cell = e.closest('td'); const prev = cell && cell.previousElementSibling;
    return prev ? norm(prev.innerText) : '';
  };
  const name = (e) => {
    const r = role(e);
    if (r === 'textbox' || r === 'combobox') return norm(e.getAttribute('aria-label')) || (e.labels && e.labels[0] ? norm(e.labels[0].innerText) : '') || caption(e);
    if (e.tagName === 'INPUT') return norm(e.getAttribute('aria-label') || e.value);
    return norm(e.getAttribute('aria-label') || e.innerText || e.textContent).slice(0, 80);
  };
  const isPassword = (e) => e.tagName === 'INPUT' && type(e) === 'password';
  window.__cuaEls = window.__cuaEls || {}; let seq = 0;
  const send = (kind, el, extra) => {
    if (!window.__cuaRecord || !el || isPassword(el)) return;
    const id = String(++seq); window.__cuaEls[id] = el;
    try { window.__cuaRecord(Object.assign({ kind, role: role(el), name: name(el), masked: masked(el), id }, extra || {})); } catch (_) {}
  };
  document.addEventListener('click', (e) => {
    const el = e.target instanceof Element ? (e.target.closest(INTERACTIVE) || e.target) : null;
    if (el && !(el.tagName === 'SELECT' || (el.tagName === 'INPUT' && TEXT_TYPES.includes(type(el))) || el.tagName === 'TEXTAREA')) send('click', el);
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!(el instanceof Element)) return;
    if (el.tagName === 'SELECT') send('select', el, { option: norm(el.selectedOptions[0] ? el.selectedOptions[0].text : '') });
    else if (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && TEXT_TYPES.includes(type(el)))) send('fill', el, { length: (el.value || '').length });
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target instanceof Element && e.target.matches('input, select')) send('press', e.target, { key: 'Enter' });
  }, true);
})();
"""


@dataclass
class HumanElement:
    """What the human acted on, for discovery's trace. IN MEMORY ONLY: never logged or persisted.

    `name` is the raw accessible name (None for text fields and selects, whose caption is not their
    name); `option` the chosen option of a select. A typed value is NOT here: it never left the page.
    """

    handle: ElementHandle
    role: str
    name: str | None
    option: str | None = None


OnAction = Callable[[HumanAction, HumanElement | None], Awaitable[None]]


def listener_script(mask_selectors: Sequence[str]) -> str:
    return _LISTENER_TEMPLATE.replace("%MASK%", json.dumps(list(mask_selectors)))


class HumanRecorder:
    def __init__(
        self,
        context: BrowserContext,
        control: SessionControl,
        logger: RunLogger,
        *,
        mask_selectors: Sequence[str] = (),
        on_action: OnAction | None = None,
    ) -> None:
        self.context = context
        self.control = control
        self.logger = logger
        self.script = listener_script(mask_selectors)
        self.on_action = on_action  # discovery: turn the element into a trace step (stage 4)
        self.actions: list[HumanAction] = []  # everything recorded this run, redacted
        self._pages: set[Page] = set()
        self._inflight = 0  # events being handled right now

    @property
    def recording(self) -> bool:
        return self.control.state in (ControlState.HUMAN, ControlState.RESUMING)

    async def drain(self, timeout_ms: int = 2_000) -> None:
        """After hand-back: let events the page sent while the human held control finish (bounded).

        A round trip to every frame delivers what was sent before it; then wait for handlers in flight.
        """
        for page in self.context.pages:
            for frame in page.frames:
                try:
                    await frame.evaluate("0")
                except PlaywrightError:
                    continue

        async def idle() -> bool:
            return self._inflight == 0

        await poll_until(idle, timeout_ms, 20)

    async def install(self) -> None:
        """Before any page loads (i.e. before login): every new document gets the listener."""
        await self.context.expose_binding(BINDING, self._on_event)
        await self.context.add_init_script(self.script)
        for page in self.context.pages:
            self._watch(page)
        self.context.on("page", self._watch)

    async def arm(self) -> None:
        """At take-control: make sure documents that were already open listen too (idempotent)."""
        for page in self.context.pages:
            for frame in page.frames:
                try:
                    await frame.evaluate(self.script)
                except PlaywrightError:
                    continue  # frame navigating: its new document gets the init script

    def since(self, index: int) -> list[HumanAction]:
        return self.actions[index:]

    # ---- events ----------------------------------------------------------------------------------- #

    def _watch(self, page: Page) -> None:
        if page not in self._pages:
            self._pages.add(page)
            page.on("framenavigated", self._on_navigated)

    def _on_navigated(self, frame: Frame) -> None:
        if not self.recording or frame.url in ("", "about:blank"):
            return
        where = frame.name or "top"
        self._add(HumanAction(at=_now(), kind="navigate", target_hint=f"{where} → {_path(frame.url)}"))

    async def _on_event(self, source: dict[str, Any], info: dict[str, Any]) -> None:
        self._inflight += 1
        try:
            await self._handle(source, info)
        finally:
            self._inflight -= 1

    async def _handle(self, source: dict[str, Any], info: dict[str, Any]) -> None:
        frame: Frame | None = source.get("frame")
        element_id = str(info.get("id", ""))
        if not self.recording:
            await _forget(frame, element_id)
            return  # automation's own actions (or a stale event) are not the human's
        kind = str(info.get("kind"))
        # A field's label is its caption, outside the field: the mask applies to text the element SHOWS.
        shows_own_text = info.get("role") not in ("textbox", "combobox")
        label = page_value(str(info.get("name") or ""), masked=bool(info.get("masked")) and shows_own_text)
        label = label or "(unnamed)"
        hint = f"{info.get('role')} '{label if '«' in label else redact_digit_runs(label)}'"
        value: str | None = None
        if kind == "fill":
            value = redact_typed("x" * int(info.get("length") or 0))  # only the length ever left the page
        elif kind == "select":
            value = page_value(str(info.get("option") or ""), masked=bool(info.get("masked")))
        elif kind == "press":
            value = str(info.get("key") or "")
        action = HumanAction(at=_now(), kind=kind, target_hint=hint, value=value)  # type: ignore[arg-type]
        self._add(action)
        handle = await _take(frame, element_id) if self.on_action is not None else None
        await _forget(frame, element_id)
        if self.on_action is not None:
            role = str(info.get("role") or "")
            element = (
                HumanElement(
                    handle=handle,
                    role=role,
                    name=str(info.get("name") or "") or None if shows_own_text else None,
                    option=str(info.get("option") or "") or None if kind == "select" else None,
                )
                if handle is not None
                else None
            )
            await self.on_action(action, element)

    def _add(self, action: HumanAction) -> None:
        self.actions.append(action)
        self.logger.event("human_action", kind=action.kind, target=action.target_hint, value=action.value)


async def _take(frame: Frame | None, element_id: str) -> ElementHandle | None:
    """The element the listener reported, from its own frame. None if the document is already gone."""
    if frame is None or not element_id:
        return None
    try:
        handle = await frame.evaluate_handle("id => (window.__cuaEls || {})[id] || null", element_id)
    except PlaywrightError:
        return None
    return handle.as_element()


async def _forget(frame: Frame | None, element_id: str) -> None:
    if frame is None or not element_id:
        return
    try:
        await frame.evaluate("id => { if (window.__cuaEls) delete window.__cuaEls[id]; }", element_id)
    except PlaywrightError:
        pass  # navigated away: the map went with the document


def _path(url: str) -> str:
    return redact_digit_runs(urlsplit(url).path or "/")  # never the query string: it can carry data


def _now() -> datetime:
    return datetime.now(UTC)
