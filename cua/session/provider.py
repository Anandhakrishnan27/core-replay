"""Session provider: owns authentication.

- Reads credentials from env (names come from the tenant config); never logs or persists them.
- Logs in before discovery/replay, so the LLM never sees the login form or credentials.
- reauthenticate() is used by the `reauthenticate` condition handler on session expiry.
- Typing happens inside `session.untraced()`, so a Playwright trace never contains the password.
- The documented exception to "all browser interaction goes through Surface": it uses the same
  context and page, and the context's network allowlist still applies.

The sign-on selectors are product-specific (one provider per product, as with an SSO adapter).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from cua.config import Tenant
from cua.surface.browser import BrowserSession

_USER_FIELD = "form[name='signon'] input[name='userid']"
_PASSWORD_FIELD = "form[name='signon'] input[name='password']"
_SUBMIT = "form[name='signon'] input[type='submit']"
_CONSOLE_PATH = "/console"
_CONSOLE_FRAME = "nav"


class MissingCredentials(Exception): ...


class LoginFailed(Exception): ...


class SessionProvider:
    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant

    def _credentials(self) -> tuple[str, str]:
        user = os.getenv(self.tenant.credentials.username_env)
        pwd = os.getenv(self.tenant.credentials.password_env)
        if not user or not pwd:
            raise MissingCredentials(
                f"set {self.tenant.credentials.username_env} / {self.tenant.credentials.password_env}"
            )
        return user, pwd

    async def _sign_on(self, session: BrowserSession, user: str, pwd: str) -> bool:
        page = session.page
        base = self.tenant.base_url.rstrip("/")
        async with session.untraced():
            try:
                await page.goto(f"{base}/login")
                await page.fill(_USER_FIELD, user)
                await page.fill(_PASSWORD_FIELD, pwd)
                async with page.expect_navigation():
                    await page.click(_SUBMIT)
            except PlaywrightError:
                return False  # Playwright messages may echo the typed value: never forward them
        return self._console_reachable(page)

    @staticmethod
    def _console_reachable(page: Page) -> bool:
        return urlsplit(page.url).path == _CONSOLE_PATH and page.frame(name=_CONSOLE_FRAME) is not None

    async def login(self, session: BrowserSession) -> None:
        """goto {base_url}/login, fill, submit, assert console reachable. Raises LoginFailed."""
        user, pwd = self._credentials()
        if not await self._sign_on(session, user, pwd):
            raise LoginFailed("sign-on did not reach the console")

    async def reauthenticate(self, session: BrowserSession) -> bool:
        """One login attempt in the SAME context; True if console reachable. Never raises.

        The attempt budget (handler max_attempts) is enforced by the caller.
        """
        try:
            user, pwd = self._credentials()
        except MissingCredentials:
            return False
        return await self._sign_on(session, user, pwd)
