"""Session provider: owns authentication. Phase 2.

- Reads credentials from env (names come from the tenant config); never logs or persists them.
- Logs in before discovery/replay, so the LLM never sees the login form or credentials.
- reauthenticate() is used by the `reauthenticate` condition handler on session expiry.
"""

from __future__ import annotations

import os
from typing import Any

from cua.config import Tenant


class MissingCredentials(Exception): ...


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

    async def login(self, page: Any) -> None:
        """TODO(phase-2): goto {base_url}/login, fill, submit, assert console reachable."""
        raise NotImplementedError

    async def reauthenticate(self, page: Any) -> bool:
        """TODO(phase-3): login again in the SAME context; return True if console reachable."""
        raise NotImplementedError
