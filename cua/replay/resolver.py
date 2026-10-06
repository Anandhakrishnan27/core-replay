"""Wait for a Target to resolve to exactly one element; map failures to the error taxonomy.

The ranked-locator loop and MatchRule live in `Surface.resolve()` (single-shot, it never waits).
This module adds the bounded wait (`poll_until`, step.timeout_ms) and turns the last
TargetNotFound / TargetAmbiguous into a Failure. The winning locator index is logged by the
Surface; index > 0 is a drift signal per (tenant, capability, target).
"""

from __future__ import annotations

from typing import Protocol

from cua.schema.artifact import FailureCategory, Target
from cua.schema.result import Failure
from cua.surface.base import Resolved, TargetAmbiguous, TargetNotFound
from cua.surface.wait import poll_until


class Resolver(Protocol):
    async def resolve(self, target_id: str, target: Target) -> Resolved: ...


async def resolve_target(
    surface: Resolver, target_id: str, target: Target, *, timeout_ms: int, poll_interval_ms: int
) -> Resolved | Failure:
    resolved: Resolved | None = None
    last: Exception | None = None

    async def attempt() -> bool:
        nonlocal resolved, last
        try:
            resolved = await surface.resolve(target_id, target)
        except (TargetNotFound, TargetAmbiguous) as e:
            last = e
            return False
        return True

    if await poll_until(attempt, timeout_ms, poll_interval_ms) and resolved is not None:
        return resolved
    category = (
        FailureCategory.TARGET_AMBIGUOUS
        if isinstance(last, TargetAmbiguous)
        else FailureCategory.TARGET_NOT_FOUND
    )
    return Failure(
        category=category,
        expected=f"exactly one element for target '{target_id}' ({target.description})",
        observed=str(last) if last else f"target '{target_id}' did not resolve",
        retryable=False,
    )
