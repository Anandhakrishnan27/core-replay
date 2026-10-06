"""End-to-end replay against the mock bank with fault injection. Enable once phases 1–3 exist."""

import pytest

pytestmark = pytest.mark.skip(reason="TODO(phase-3): needs mockbank + executor")

CASES = [
    # (member_id, fault,            expected status,     outcome_code / failure category)
    ("10002", None, "success", "SUCCESS"),
    ("99999", None, "business_outcome", "MEMBER_NOT_FOUND"),
    ("10003", None, "business_outcome", "NO_SAVINGS_ACCOUNT"),
    ("10001", "notice", "success", "SUCCESS"),  # recovery: dismiss
    ("10001", "slow", "success", "SUCCESS"),  # recovery: retry
    ("10001", "session_expired", "success", "SUCCESS"),  # recovery: reauthenticate
    ("10001", "denied", "failed", "PERMISSION_DENIED"),
    ("10001", "error", "failed", "APP_ERROR"),
    ("10001", "maint", "failed", "UNKNOWN_STATE"),  # escalates; auto-abort in test
]


@pytest.mark.parametrize("member_id,fault,status,code", CASES)
async def test_replay(member_id, fault, status, code): ...
