"""End-to-end replay against the mock bank with fault injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cua.replay.executor as executor
from cua.replay.executor import execute
from cua.schema.result import RunStatus
from mockbank.data import find_member

CASES = [
    # (member_id, fault,            expected status,     outcome_code / failure category)
    ("10002", None, "success", "SUCCESS"),
    ("99999", None, "business_outcome", "MEMBER_NOT_FOUND"),
    ("10003", None, "business_outcome", "NO_SAVINGS_ACCOUNT"),
    ("10001", "not_found", "business_outcome", "MEMBER_NOT_FOUND"),
    ("10001", "notice", "success", "SUCCESS"),  # recovery: dismiss
    ("10001", "slow", "success", "SUCCESS"),  # recovery: wait_until_clear
    ("10001", "session_expired", "success", "SUCCESS"),  # recovery: reauthenticate
    ("10001", "denied", "failed", "PERMISSION_DENIED"),
    ("10001", "error", "failed", "APP_ERROR"),
    ("10001", "maint", "failed", "UNKNOWN_STATE"),  # escalates; no operator → ends at once
]


@pytest.fixture
def run(approved_artifact, tenant, test_policy, browser, tmp_path):
    async def go(member_id: str, fault: str | None = None, **kw):
        return await execute(
            kw.pop("artifact", approved_artifact),
            kw.pop("tenant", tenant),
            test_policy,
            {"member_id": member_id},
            fault=fault,
            browser=kw.pop("browser", browser),
            evidence_root=tmp_path,
            **kw,
        )

    return go


def events(result) -> list[dict]:
    path = Path(result.evidence_dir) / "run.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("member_id,fault,status,code", CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
async def test_replay(run, member_id, fault, status, code):
    result = await run(member_id, fault)
    assert result.status.value == status
    if status in ("success", "business_outcome"):
        assert result.outcome_code == code
    else:
        assert result.failure is not None
        assert result.failure.category.value == code
    assert (Path(result.evidence_dir) / "result.json").exists()


async def test_success_outputs_are_exact(run):
    result = await run("10002")
    savings = next(a for a in find_member("10002").accounts if a.account_type == "Share Savings")
    assert result.outputs == {"savings_balance": "1203.55", "account_status": savings.status}
    assert savings.balance == "$1,203.55"
    assert result.recoveries == [] and result.handoffs == []


@pytest.mark.parametrize(
    "fault,condition,action",
    [
        ("notice", "system_notice", "dismiss"),
        ("slow", "still_processing", "wait_until_clear"),
        ("session_expired", "session_expired", "reauthenticate"),
    ],
)
async def test_recoveries_are_recorded(run, fault, condition, action):
    result = await run("10001", fault)
    assert result.status is RunStatus.success
    assert result.outputs["savings_balance"] == "2450.17"
    assert result.recoveries, "expected a recovery"
    assert {(r.condition_id, r.action) for r in result.recoveries} == {(condition, action)}
    assert result.recoveries[-1].succeeded


async def test_reauthenticate_happens_once(run):
    result = await run("10001", "session_expired")
    assert [r.attempt for r in result.recoveries] == [1]


async def test_unknown_state_snapshots_and_escalates(run):
    result = await run("10001", "maint")
    assert result.failure is not None
    assert result.failure.step_id == "submit_search"
    assert [h.resolution for h in result.handoffs] == ["aborted"]  # no operator available
    assert result.handoffs[0].operator_id is None and result.handoffs[0].human_actions == []
    assert any(p.endswith(".png") for p in result.failure.evidence)
    assert any(".dom." in p for p in result.failure.evidence)
    names = [e["event"] for e in events(result)]
    assert names.index("escalation_requested") < names.index("handoff_resolved")
    resolved = next(e for e in events(result) if e["event"] == "handoff_resolved")
    assert resolved["details"]["detail"] == "no operator available"


async def test_hard_failure_does_not_escalate(run):
    result = await run("10001", "denied")
    assert result.handoffs == []
    assert result.failure is not None and not result.failure.retryable


async def test_business_outcome_has_no_failure(run):
    result = await run("10003")
    assert result.failure is None and result.outputs == {}


# ---- pre-flight: refused before the UI, no browser launched ----------------------------------- #


@pytest.fixture
def no_browser(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("browser must not start for a rejected run")

    monkeypatch.setattr(executor, "start_browser", boom)


async def test_invalid_input_rejected(run, no_browser):
    result = await run("12ab", browser=None)
    assert result.status is RunStatus.rejected
    assert result.failure is not None and result.failure.category.value == "INPUT_INVALID"


async def test_draft_artifact_rejected_unattended(run, artifact, no_browser):
    result = await run("10001", artifact=artifact, browser=None)
    assert result.status is RunStatus.rejected
    assert result.failure is not None and result.failure.category.value == "POLICY_VIOLATION"


async def test_draft_artifact_runs_supervised(run, artifact):
    result = await run("10001", artifact=artifact, mode="supervised")
    assert result.status is RunStatus.success


async def test_tenant_version_outside_range_rejected(run, tenant, no_browser):
    result = await run("10001", tenant=tenant.model_copy(update={"product_version": "3.1"}), browser=None)
    assert result.status is RunStatus.rejected
    assert result.failure is not None and result.failure.category.value == "APP_VERSION_MISMATCH"


async def test_secrets_template_rejected(run, raw_artifact, no_browser):
    from cua.schema.artifact import CapabilityArtifact

    raw_artifact["steps"][2]["action"]["value"] = "{{secrets.api_key}}"
    result = await run(
        "10001", artifact=CapabilityArtifact.model_validate(raw_artifact), mode="supervised", browser=None
    )
    assert result.status is RunStatus.rejected


# ---- evidence: no raw PII anywhere --------------------------------------------------------------- #


def assert_no_raw_pii(root: Path) -> None:
    assert (root / "run.jsonl").exists() and (root / "artifact.json").exists()
    assert any((root / "steps").glob("*.png"))
    for path in root.rglob("*"):
        if path.suffix in (".json", ".jsonl", ".html"):
            text = path.read_text()
            for raw in (
                "10002",
                "1,203.55",
                "1203.55",
                "Marlowe Tenby",
                "900-58-7731",
                "marlowe.tenby@example.test",
            ):
                assert raw not in text, f"{raw!r} leaked into {path.name}"


async def test_evidence_has_no_raw_pii(run):
    ok = await run("10002")
    failed = await run("10002", "maint")
    assert ok.outputs["savings_balance"] == "1203.55"  # the caller gets the real value ...
    for result in (ok, failed):
        assert_no_raw_pii(Path(result.evidence_dir))
    stored = json.loads((Path(ok.evidence_dir) / "result.json").read_text())
    assert stored["outputs"]["savings_balance"].startswith("«savings_balance:sha256:")  # ... evidence doesn't
    assert stored["outputs"]["account_status"] == "Active"
