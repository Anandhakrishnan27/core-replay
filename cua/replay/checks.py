"""Predicate evaluation and the post-action race.

Fixed precedence, polled every limits.poll_interval_ms until step.expect.timeout_ms:
    1. watched conditions in declared order → first match → Matched(condition_id)
    2. step.expect satisfied (or no expect) → Satisfied
timeout with nothing matched → TimedOut (the executor turns it into UNKNOWN_STATE + escalate).

This module only DETECTS. Handling (outcomes, recoveries, escalation) lives in the executor.
A step without `expect` gets a single poll: conditions are checked once, then the step is done.
`KnownCondition.detect.timeout_ms` is not used here; the step's deadline governs the race.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from cua.schema.artifact import Check, KnownCondition, Predicate, Step, Target, TargetAbsent
from cua.surface.wait import poll_until


class Checker(Protocol):
    async def check(self, predicate: Predicate, targets: dict[str, Target]) -> bool: ...


@dataclass(frozen=True)
class Satisfied:
    pass


@dataclass(frozen=True)
class Matched:
    condition_id: str


@dataclass(frozen=True)
class TimedOut:
    pass


RaceResult = Satisfied | Matched | TimedOut


async def evaluate(surface: Checker, check: Check, targets: dict[str, Target]) -> bool:
    """One pass: every `all_of` predicate holds and, if `any_of` is given, at least one of those."""
    for p in check.all_of:
        if not await surface.check(p, targets):
            return False
    if not check.any_of:
        return True
    for p in check.any_of:
        if await surface.check(p, targets):
            return True
    return False


def watched(conditions: Mapping[str, KnownCondition], step_id: str) -> list[tuple[str, KnownCondition]]:
    """Conditions watched after `step_id`, in declared order."""
    return [(cid, c) for cid, c in conditions.items() if c.applies_to == "all" or step_id in c.applies_to]


def _needs_confirmation(condition: KnownCondition) -> bool:
    return any(isinstance(p, TargetAbsent) for p in condition.detect.predicates())


async def match_condition(
    surface: Checker,
    conditions: list[tuple[str, KnownCondition]],
    targets: dict[str, Target],
    *,
    poll_interval_ms: int,
) -> str | None:
    """First condition (in order) whose detector holds now, or None.

    A detector that relies on `target_absent` must still hold one poll interval later: a header
    painted before its table must not read as "no savings account".
    """
    for cid, cond in conditions:
        if not await evaluate(surface, cond.detect, targets):
            continue
        if _needs_confirmation(cond):

            async def cleared(c: KnownCondition = cond) -> bool:
                return not await evaluate(surface, c.detect, targets)

            if await poll_until(cleared, poll_interval_ms, poll_interval_ms):
                return None  # it was transient: let the race poll again
        return cid
    return None


async def race(
    surface: Checker,
    step: Step,
    conditions: Mapping[str, KnownCondition],
    targets: dict[str, Target],
    *,
    poll_interval_ms: int,
) -> RaceResult:
    watch = watched(conditions, step.id)
    result: RaceResult = TimedOut()

    async def poll() -> bool:
        nonlocal result
        cid = await match_condition(surface, watch, targets, poll_interval_ms=poll_interval_ms)
        if cid is not None:
            result = Matched(cid)
            return True
        if step.expect is None or await evaluate(surface, step.expect, targets):
            result = Satisfied()
            return True
        return False

    timeout_ms = step.expect.timeout_ms if step.expect is not None else 0
    await poll_until(poll, timeout_ms, poll_interval_ms)
    return result
