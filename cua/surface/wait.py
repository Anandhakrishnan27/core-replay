"""The one bounded predicate wait. No free-standing sleeps anywhere else (invariant 8)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def poll_until(fn: Callable[[], Awaitable[bool]], timeout_ms: int, interval_ms: int) -> bool:
    """Evaluate `fn` until it returns True or `timeout_ms` elapses. Always evaluates at least once."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        if await fn():
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(interval_ms / 1000, remaining))
