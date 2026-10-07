"""PlaywrightWebSurface: Surface implementation for web and legacy web apps.

Responsibilities:
  - SessionControl.ensure_automation() (when a control is attached) and PolicyGate.authorize()
    before every act()
  - descend frame_path; resolve ranked locators + MatchRule; Playwright auto-waits for actionability
  - single-shot predicate checks (polling/race lives in cua.replay.checks)
  - masked screenshots (sensitive targets + tenant mask_selectors), redacted DOM dumps
The network allowlist and tracing live on the context (cua.surface.browser), so they cover login too.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as PWLocator

from cua.config import Policy
from cua.evidence.logger import RunLogger
from cua.handoff.controller import SessionControl
from cua.safety.policy import Mode, NeedsHuman, PolicyGate, PolicyViolation
from cua.safety.redact import data_shape, page_value, redact_digit_runs
from cua.schema.artifact import (
    Action,
    Click,
    CoordinateLocator,
    Extract,
    Fill,
    Locator,
    MatchRule,
    Navigate,
    Predicate,
    Press,
    RiskClass,
    SelectOption,
    Target,
    TargetAbsent,
    TargetVisible,
    TextPresent,
    UrlMatches,
)
from cua.schema.trace import ElementSnapshot, PageState, TableContext
from cua.surface.aria import (
    ELEMENT_JS,
    FRAME_NAME_JS,
    INPUT_ROLES,
    PAGE_TEXT_JS,
    AriaLine,
    parse_line,
    redact_snapshot,
)
from cua.surface.base import ActionFailed, Observation, Resolved, TargetAmbiguous, TargetNotFound
from cua.surface.locators import element_at, frames_for, to_playwright

MASK_COLOR = "#FF00FF"

_TEXT_JS = (
    "e => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) ? e.value : (e.innerText ?? e.textContent)"
)
_PATHS_JS = """els => els.map(e => {
  const p = [];
  while (e && e.parentElement) {
    p.unshift(Array.prototype.indexOf.call(e.parentElement.children, e));
    e = e.parentElement;
  }
  return p;
})"""
# Clone the document (never mutate the live page), then mask: sensitive targets by index path,
# mask_selectors by CSS, and every form value.
_SERIALIZE_JS = """({paths, selectors}) => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('*').forEach((e) => { if (e.tagName.startsWith('X-PW-')) e.remove(); });
  const mask = (e) => { e.textContent = '\\u00abmasked\\u00bb'; };
  for (const p of paths) { let e = clone; for (const i of p) { e = e && e.children[i]; } if (e) mask(e); }
  for (const s of selectors) { try { clone.querySelectorAll(s).forEach(mask); } catch (_) {} }
  clone.querySelectorAll('input, textarea, select, option').forEach((e) => {
    e.removeAttribute('value');
    if (e.tagName === 'TEXTAREA') e.textContent = '';
  });
  return clone.outerHTML;
}"""
_RAW_BLOCK_RE = re.compile(r"(<(style|script)\b.*?</\2>)", re.S | re.I)
# Discovery has no `sensitive` targets yet, so its screenshots mask every leaf element showing a digit
# and every text-entry control. Over-masking is the safe direction.
_DIGIT_RE = re.compile(r"\d")
_LEAF = "body *:not(:has(*))"
_TEXT_ENTRY = (
    "input:not([type]), input[type=text], input[type=search], input[type=number], input[type=tel], "
    "input[type=email], input[type=password], textarea"
)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "snapshot"


def _redact_html(html: str) -> str:
    """Hash long digit runs in markup and text, leaving <style>/<script> bodies (colors, code) alone."""
    parts = _RAW_BLOCK_RE.split(html)
    out: list[str] = []
    i = 0
    while i < len(parts):
        out.append(redact_digit_runs(parts[i]))
        if i + 1 < len(parts):
            out.append(parts[i + 1])  # the raw block, kept verbatim
        i += 3
    return "".join(out)


class PlaywrightWebSurface:
    def __init__(
        self,
        page: Page,
        policy: Policy,
        gate: PolicyGate,
        logger: RunLogger,
        *,
        mode: Mode,
        confirmed: bool = False,
        targets: dict[str, Target] | None = None,
        mask_selectors: Sequence[str] = (),
        control: SessionControl | None = None,
    ) -> None:
        self.page = page
        self.policy = policy
        self.gate = gate
        self.logger = logger
        self.mode: Mode = mode
        self.confirmed = confirmed
        self.targets = targets or {}
        self.mask_selectors = list(mask_selectors)
        self.control = control
        self._snapshots = 0

    # ---- observe ------------------------------------------------------------------------------- #

    async def observe(self, with_screenshot: bool = False) -> Observation:
        """Discovery's view of the page: redacted aria tree with refs, an ElementSnapshot per ref, PageState.

        One ai-mode aria snapshot covers the top document and every frame. Nothing in the result holds a
        typed value, a masked value or a data value (see cua.surface.aria).
        """
        raw = await self.page.aria_snapshot(mode="ai")
        refs: dict[str, ElementSnapshot] = {}
        safe_names: dict[str, str] = {}
        frame_names: dict[str, str] = {}
        for raw_line in raw.splitlines():
            line = parse_line(raw_line)
            if line is None or line.ref is None:
                continue
            ref = line.ref
            if line.role == "iframe":
                handle = await self._ref_handle(ref)
                if handle is not None:
                    frame_names[ref] = str(await handle.evaluate(FRAME_NAME_JS))
                continue
            if not line.wants_snapshot:
                continue
            snap = await self._element_snapshot(ref, line)
            if snap is None:
                continue
            refs[ref] = snap
            # A control's trailing text is its typed value: never a display name.
            shown = line.name if line.role in INPUT_ROLES else (line.name or line.text)
            if shown:
                safe_names[ref] = snap.accessible_name or snap.text or page_value(shown)
        text = redact_snapshot(raw, safe_names, frame_names)
        png = None
        if with_screenshot:
            png = await self.page.screenshot(
                mask=self._mask_locators(), mask_color=MASK_COLOR, animations="disabled"
            )
        return Observation(page=await self._page_state(), aria_snapshot=text, refs=refs, screenshot_png=png)

    async def _ref_handle(self, ref: str) -> ElementHandle | None:
        """The live element for an aria ref from the latest snapshot. Never waits."""
        try:
            handles = await self.page.locator(f"aria-ref={ref}").element_handles()
        except PlaywrightError:
            return None
        return handles[0] if len(handles) == 1 else None

    async def _element_snapshot(self, ref: str, line: AriaLine) -> ElementSnapshot | None:
        handle = await self._ref_handle(ref)
        if handle is None:
            return None
        try:
            info = await handle.evaluate(ELEMENT_JS, self.mask_selectors)
            frame = await handle.owner_frame()
            box = await handle.bounding_box()
        except PlaywrightError:
            return None  # detached mid-observation: the next observation will have it
        masked = bool(info["masked"])

        def safe(text: str | None, is_masked: bool = masked) -> str | None:
            return page_value(text, masked=is_masked) if text else None

        nearby: dict[str, str] = {}
        if info["left"]:
            nearby["left"] = page_value(info["left"]["text"], masked=info["left"]["masked"])
        table = None
        if info["table"]:
            t = info["table"]
            nearby["above"] = t["column"]
            table = TableContext(
                table_headers=t["headers"],
                row_text=" | ".join(page_value(c["text"], masked=c["masked"]) for c in t["row"]),
                column_header=t["column"],
            )
        return ElementSnapshot(
            frame_path=_frame_path(frame),
            tag=info["tag"],
            role=line.role,
            accessible_name=safe(line.name),
            label=safe(info["label"], False),
            name_attr=info["name_attr"],
            id_attr=info["id_attr"],
            text=safe(info["text"]),
            nearby_text=nearby,
            table_context=table,
            css_path=info["css"],
            xpath=info["xpath"],
            bbox=(box["x"], box["y"], box["width"], box["height"]) if box else None,
        )

    async def _page_state(self) -> PageState:
        headings: list[str] = []
        texts: list[str] = []
        for frame in self.page.frames:
            try:
                found = await frame.evaluate(PAGE_TEXT_JS, self.mask_selectors)
            except PlaywrightError:
                continue
            headings.extend(found["headings"])
            texts.extend(found["texts"])

        def ui_only(items: list[str], limit: int) -> list[str]:
            # dict.fromkeys: dedupe, keep first-seen order
            return [t for t in dict.fromkeys(items) if t and len(t) <= limit and data_shape(t) is None]

        parts = urlsplit(self.page.url)
        return PageState(
            url=f"{parts.scheme}://{parts.netloc}{parts.path}",
            title=redact_digit_runs(await self.page.title()),
            headings=ui_only(headings, 120),
            visible_texts=ui_only(texts, 80),
        )

    # ---- resolve ------------------------------------------------------------------------------- #

    async def _candidates(self, target: Target, locator: Locator) -> list[ElementHandle]:
        """Current elements for one locator, across the target's frames. A snapshot: never waits."""
        if isinstance(locator, CoordinateLocator):
            handle = await element_at(self.page, locator.x, locator.y)
            return [handle] if handle is not None else []
        found: list[ElementHandle] = []
        for frame in frames_for(self.page, target.frame_path):
            try:
                found.extend(await to_playwright(frame, locator).element_handles())
            except PlaywrightError:
                continue  # frame navigating or detached: no elements from it right now
        return found

    async def _text(self, handle: ElementHandle) -> str:
        return _norm(str(await handle.evaluate(_TEXT_JS) or ""))

    async def _passes(self, handle: ElementHandle, rule: MatchRule) -> bool:
        try:
            if rule.visible and not await handle.is_visible():
                return False
            if rule.enabled is not None and await handle.is_enabled() != rule.enabled:
                return False
            if rule.text_pattern is not None and not re.search(rule.text_pattern, await self._text(handle)):
                return False
        except PlaywrightError:
            return False
        return True

    async def _resolve(self, target_id: str, target: Target, *, log: bool) -> Resolved:
        ambiguous = False
        for index, locator in enumerate(target.locators):
            candidates = await self._candidates(target, locator)
            if not candidates:
                continue
            if len(candidates) > 1 and target.match.unique:
                ambiguous = True
                continue
            if await self._passes(candidates[0], target.match):
                if log:
                    self.logger.event(
                        "target_resolved", target=target_id, strategy=locator.strategy, locator_index=index
                    )
                return Resolved(target_id=target_id, locator_index=index, handle=candidates[0])
        # Never include element text in these messages: a failed text_pattern may be a balance.
        if ambiguous:
            raise TargetAmbiguous(f"target '{target_id}': a locator matched more than one element")
        raise TargetNotFound(f"target '{target_id}': no locator matched one element passing its match rule")

    async def resolve(self, target_id: str, target: Target) -> Resolved:
        """Try locators in order; first that matches EXACTLY ONE element and passes MatchRule wins.

        Single-shot (does not wait). locator_index > 0 is logged and is a drift signal.
        """
        return await self._resolve(target_id, target, log=True)

    # ---- act / read ---------------------------------------------------------------------------- #

    async def _frame_url(self, resolved: Resolved | None) -> str:
        if resolved is not None:
            frame: Frame | None = await resolved.handle.owner_frame()
            if frame is not None:
                return frame.url
        return self.page.url

    def _authorize(self, action_type: str, risk: RiskClass, url: str, target_id: str | None) -> None:
        if self.control is not None:
            self.control.ensure_automation()
        try:
            self.gate.authorize(
                action_type=action_type, risk=risk, mode=self.mode, url=url, confirmed=self.confirmed
            )
        except (PolicyViolation, NeedsHuman) as e:
            self.logger.event(
                "policy_blocked", action=action_type, target=target_id, risk=risk.value, reason=str(e)
            )
            raise

    async def act(
        self, action: Action, resolved: Resolved | None, risk: RiskClass, *, value: str | None
    ) -> None:
        """Policy-gated action. `value` is the already-resolved template value (never logged raw)."""
        if isinstance(action, Extract):
            raise TypeError("extract is not an act(): use read()")
        target_id = resolved.target_id if resolved is not None else None
        if isinstance(action, Navigate):
            if value is None:
                raise ValueError("navigate needs the resolved URL as `value`")
            url = value
        else:
            url = await self._frame_url(resolved)
        self._authorize(action.type, risk, url, target_id)
        if isinstance(action, Click | Fill | SelectOption) and resolved is None:
            raise ValueError(f"{action.type} needs a resolved target")

        try:
            if isinstance(action, Navigate):
                await self.page.goto(url)
            elif isinstance(action, Click):
                assert resolved is not None
                await resolved.handle.click()
            elif isinstance(action, Fill):
                assert resolved is not None
                if value is None:
                    raise ValueError("fill needs a value")
                await resolved.handle.fill(value)
            elif isinstance(action, SelectOption):
                assert resolved is not None
                await resolved.handle.select_option(label=action.option)
            elif isinstance(action, Press):
                if resolved is not None:
                    await resolved.handle.press(action.key)
                else:
                    await self.page.keyboard.press(action.key)
        except PlaywrightError as e:
            # Playwright messages can echo arguments, so fill never forwards them.
            detail = "" if isinstance(action, Fill) else f": {str(e).splitlines()[0]}"
            raise ActionFailed(f"{action.type} on '{target_id}' failed{detail}") from None
        self.logger.event("action", action=action.type, target=target_id, risk=risk.value)

    async def read(self, resolved: Resolved) -> str:
        """Visible text (or form value) of the element, whitespace-normalized. Never logged here."""
        self._authorize("extract", RiskClass.read, await self._frame_url(resolved), resolved.target_id)
        try:
            return await self._text(resolved.handle)
        except PlaywrightError as e:
            raise ActionFailed(f"extract on '{resolved.target_id}' failed: {type(e).__name__}") from None

    # ---- check --------------------------------------------------------------------------------- #

    async def check(self, predicate: Predicate, targets: dict[str, Target]) -> bool:
        """Evaluate one predicate NOW. Transient errors (a frame mid-navigation) → False; caller polls."""
        try:
            if isinstance(predicate, UrlMatches):
                # frame URLs too: legacy apps navigate inside frames (e.g. /login inside `main`)
                return any(re.search(predicate.pattern, f.url) for f in self.page.frames)
            if isinstance(predicate, TargetVisible):
                return await self._resolves(predicate.target, targets[predicate.target])
            if isinstance(predicate, TargetAbsent):
                return await self._absent(targets[predicate.target])
            if isinstance(predicate, TextPresent):
                return await self._text_present(predicate, targets)
        except PlaywrightError:
            return False
        raise TypeError(f"unknown predicate {predicate!r}")

    async def _resolves(self, target_id: str, target: Target) -> bool:
        try:
            await self._resolve(target_id, target, log=False)
        except (TargetNotFound, TargetAmbiguous):
            return False
        return True

    async def _absent(self, target: Target) -> bool:
        """No locator finds any visible element. Ambiguous is NOT absent."""
        for locator in target.locators:
            for handle in await self._candidates(target, locator):
                if await handle.is_visible():
                    return False
        return True

    async def _text_present(self, predicate: TextPresent, targets: dict[str, Target]) -> bool:
        needle = _norm(predicate.text)
        if predicate.within is not None:
            try:
                scope = await self._resolve(predicate.within, targets[predicate.within], log=False)
            except (TargetNotFound, TargetAmbiguous):
                return False
            return needle in await self._text(scope.handle)
        for frame in self.page.frames:
            try:
                text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            except PlaywrightError:
                continue
            if needle in _norm(str(text)):
                return True
        return False

    # ---- snapshot ------------------------------------------------------------------------------ #

    def _sensitive(self) -> list[Target]:
        if not self.policy.redaction.mask_sensitive_targets_in_screenshots:
            return []
        return [t for t in self.targets.values() if t.sensitive]

    def _mask_locators(self) -> list[PWLocator]:
        """Everything ANY locator of a sensitive target matches, plus tenant mask_selectors, in all frames.

        Over-masking is the safe direction.
        """
        masks: list[PWLocator] = []
        for target in self._sensitive():
            for frame in frames_for(self.page, target.frame_path):
                masks.extend(
                    to_playwright(frame, loc)
                    for loc in target.locators
                    if not isinstance(loc, CoordinateLocator)
                )
        for frame in self.page.frames:
            masks.extend(frame.locator(sel) for sel in self.mask_selectors)
            if self.mode == "discovery":
                masks.append(frame.locator(_LEAF, has_text=_DIGIT_RE))
                masks.append(frame.locator(_TEXT_ENTRY))
        return masks

    async def _dom(self, frame: Frame) -> str:
        paths: list[list[int]] = []
        for target in self._sensitive():
            if frame not in frames_for(self.page, target.frame_path):
                continue
            for loc in target.locators:
                if not isinstance(loc, CoordinateLocator):
                    paths.extend(await to_playwright(frame, loc).evaluate_all(_PATHS_JS))
        html = await frame.evaluate(_SERIALIZE_JS, {"paths": paths, "selectors": self.mask_selectors})
        return _redact_html(str(html))

    async def snapshot(self, reason: str, *, dom: bool = False) -> list[str]:
        """Masked screenshot (+ redacted DOM per frame). Returns evidence-relative paths.

        `reason` is used in file names, so callers pass static strings (step ids), never data.
        """
        self._snapshots += 1
        stem = f"{self._snapshots:02d}_{_slug(reason)}"
        png = self.logger.dir / "steps" / f"{stem}.png"
        await self.page.screenshot(
            path=png, full_page=True, mask=self._mask_locators(), mask_color=MASK_COLOR, animations="disabled"
        )
        paths = [png]
        if dom:
            for i, frame in enumerate(self.page.frames):
                try:
                    html = await self._dom(frame)
                except PlaywrightError:
                    continue
                path = self.logger.dir / "steps" / f"{stem}.dom.{i}_{_slug(frame.name or 'top')}.html"
                path.write_text(html, encoding="utf-8")
                paths.append(path)
        rel = [self.logger.rel(p) for p in paths]
        self.logger.event("snapshot", reason=_slug(reason), files=rel)
        return rel


def _frame_path(frame: Frame | None) -> list[str]:
    """Names of the frames from the top document down to `frame` (url path when a frame has no name)."""
    path: list[str] = []
    while frame is not None and frame.parent_frame is not None:
        path.insert(0, frame.name or urlsplit(frame.url).path)
        frame = frame.parent_frame
    return path
