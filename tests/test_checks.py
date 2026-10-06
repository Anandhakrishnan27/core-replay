"""The race, without a browser: a fake surface answers predicates from a script."""

from __future__ import annotations

import pytest

from cua.replay.checks import Matched, Satisfied, TimedOut, race
from cua.schema.artifact import Step

POLL_MS = 20


class FakeSurface:
    """Answers `check()` by predicate key. A list is consumed one answer per call; its last repeats."""

    def __init__(self, truth: dict[str, bool | list[bool]]) -> None:
        self.truth = truth
        self.calls = 0

    async def check(self, predicate, targets) -> bool:
        self.calls += 1
        subject = getattr(predicate, "text", None) or getattr(predicate, "target", None) or predicate.pattern
        key = f"{predicate.kind}:{subject}"
        answer = self.truth.get(key, False)
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer


def step(artifact, step_id: str, timeout_ms: int | None = None) -> Step:
    s = next(s for s in artifact.steps if s.id == step_id)
    if timeout_ms is not None and s.expect is not None:
        s = s.model_copy(update={"expect": s.expect.model_copy(update={"timeout_ms": timeout_ms})})
    return s


async def run_race(artifact, step_id, truth, timeout_ms=None):
    surface = FakeSurface(truth)
    result = await race(
        surface,
        step(artifact, step_id, timeout_ms),
        artifact.conditions,
        artifact.targets,
        poll_interval_ms=POLL_MS,
    )
    return result, surface


RESULT_PAGE = {"target_visible:member_header": True, "target_visible:savings_balance_cell": True}


async def test_checkpoint_satisfied(artifact):
    result, _ = await run_race(artifact, "submit_search", RESULT_PAGE)
    assert result == Satisfied()


async def test_condition_beats_checkpoint(artifact):
    result, _ = await run_race(
        artifact, "submit_search", {**RESULT_PAGE, "text_present:No member found": True}
    )
    assert result == Matched("member_not_found")


async def test_conditions_in_declared_order(artifact):
    truth = {"text_present:System Notice": True, "text_present:An unexpected error has occurred": True}
    result, _ = await run_race(artifact, "submit_search", truth)
    assert result == Matched("system_notice")  # declared before app_error


async def test_applies_to_filters_conditions(artifact):
    # "No member found" is watched only after submit_search
    truth = {"text_present:No member found": True, "target_visible:member_id_field": True}
    result, _ = await run_race(artifact, "go_to_lookup", truth)
    assert result == Satisfied()


async def test_step_without_expect_is_a_single_poll(artifact):
    result, surface = await run_race(artifact, "enter_member_id", {})
    assert result == Satisfied()
    watched = [c for c in artifact.conditions.values() if c.applies_to == "all"]
    assert surface.calls == sum(len(c.detect.predicates()) for c in watched)


async def test_timeout_when_nothing_matches(artifact):
    result, _ = await run_race(artifact, "submit_search", {}, timeout_ms=100)
    assert result == TimedOut()


@pytest.mark.parametrize(
    "absent,expected",
    [
        ([True, True, True], Matched("no_savings_account")),  # still absent one interval later
        ([True, False, False], Satisfied()),  # transient: the balance cell appeared
    ],
)
async def test_target_absent_needs_confirmation(artifact, absent, expected):
    visible = [not a for a in absent]
    truth = {
        "target_visible:member_header": True,
        "target_absent:savings_balance_cell": absent,
        "target_visible:savings_balance_cell": visible,
    }
    result, _ = await run_race(artifact, "submit_search", truth, timeout_ms=500)
    assert result == expected
