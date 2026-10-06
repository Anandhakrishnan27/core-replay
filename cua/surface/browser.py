"""Browser + context launcher: one isolated context per run, network allowlist, optional tracing.

    async with start_browser(headed=False) as browser:
        async with open_session(browser, policy, gate, logger) as session:
            ...

Tracing is OFF unless `trace=True`. Traces contain unmasked page content, typed values and session
cookies, so they are written only under evidence/_scratch/ (git-ignored) and never committed.
Login and re-authentication run inside `session.untraced()`, so credentials are never recorded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright

from cua.config import EVIDENCE_DIR, Policy
from cua.evidence.logger import RunLogger
from cua.safety.policy import PolicyGate
from cua.surface.engines import register_engines

VIEWPORT = {"width": 1280, "height": 800}  # fixed, so `coordinates` locators are deterministic
SCRATCH_DIR = EVIDENCE_DIR / "_scratch"


@dataclass
class BrowserSession:
    context: BrowserContext
    page: Page
    trace_dir: Path | None = None
    trace_files: list[Path] = field(default_factory=list)

    async def _resume(self) -> None:
        await self.context.tracing.start(screenshots=True, snapshots=True)

    async def _save(self) -> None:
        """Stop recording completely and write what was recorded since the last _resume()."""
        assert self.trace_dir is not None
        path = self.trace_dir / f"trace.{len(self.trace_files)}.zip"
        await self.context.tracing.stop(path=path)
        self.trace_files.append(path)

    async def _start_trace(self, trace_dir: Path) -> None:
        self.trace_dir = _ensure_dir(trace_dir)
        await self._resume()

    @asynccontextmanager
    async def untraced(self) -> AsyncIterator[None]:
        """Nothing inside this block is recorded (credentials are typed here).

        Tracing is fully stopped, not just chunked: the network recorder keeps running across chunks
        and would write the login POST body into the next chunk.
        """
        if self.trace_dir is None:
            yield
            return
        await self._save()
        try:
            yield
        finally:
            await self._resume()

    async def _stop_trace(self) -> None:
        if self.trace_dir is not None:
            await self._save()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def install_network_gate(context: BrowserContext, gate: PolicyGate, logger: RunLogger) -> None:
    """Abort every request (all frames, redirects, popups) whose origin is not allowlisted."""

    async def handler(route: Route) -> None:
        url = route.request.url
        if gate.origin_allowed(url):
            await route.continue_()
            return
        # origin only: paths and query strings can carry data
        logger.event("network_blocked", origin=_origin(url), resource_type=route.request.resource_type)
        await route.abort("blockedbyclient")

    await context.route("**/*", handler)


@asynccontextmanager
async def start_browser(*, headed: bool = False) -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        await register_engines(pw)
        browser = await pw.chromium.launch(headless=not headed)
        try:
            yield browser
        finally:
            await browser.close()


@asynccontextmanager
async def open_session(
    browser: Browser,
    policy: Policy,
    gate: PolicyGate,
    logger: RunLogger,
    *,
    trace: bool = False,
    scratch_root: Path = SCRATCH_DIR,
) -> AsyncIterator[BrowserSession]:
    """One isolated context + page for one run. The network gate is installed before any request."""
    context = await browser.new_context(
        viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
        service_workers="block",  # service-worker requests would bypass context.route
        accept_downloads=False,
    )
    session: BrowserSession | None = None
    try:
        await install_network_gate(context, gate, logger)
        context.set_default_timeout(policy.limits.step_timeout_ms)
        context.set_default_navigation_timeout(policy.limits.step_timeout_ms)
        session = BrowserSession(context=context, page=await context.new_page())
        if trace:
            await session._start_trace(scratch_root / "traces" / logger.dir.name)
            logger.event("trace_enabled", location="evidence/_scratch (not committed)")
        yield session
    finally:
        if session is not None:
            await session._stop_trace()
        await context.close()
