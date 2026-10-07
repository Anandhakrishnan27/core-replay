"""`cua discover` pipeline end to end against the mock bank, with the scripted stub as the model.

Also replays a COMPILED artifact through the whole fault matrix: compiled output must behave like the
hand-written fixture (except for the derived NO_SHARE_SAVINGS outcome name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cua.discovery.pipeline as pipeline
from cua import catalog
from cua.compiler.compile import compile_trace
from cua.discovery.pipeline import discover_capability
from cua.replay.executor import execute
from cua.schema.artifact import CapabilityArtifact, TextPresent
from cua.schema.trace import DiscoveryTrace
from tests.test_discovery_agent import GOAL, HAPPY_PATH, PARAMS, StubMessages, call

CAP_ID = "mockbank.member.lookup_savings_balance"
FIXTURE_TRACE = Path(__file__).parent / "fixtures" / "discovery_trace.notice.json"
RAW = ["10001", "2,450.17", "2450.17", "Test Member A"]


@pytest.fixture
def run_pipeline(browser, tenant, test_policy, tmp_path):
    catalog_root = tmp_path / "capabilities"

    async def go(script, **kw):
        stub = StubMessages(script)
        result = await discover_capability(
            GOAL,
            tenant,
            test_policy,
            PARAMS,
            kw.pop("capability_id", CAP_ID),
            kw.pop("version", "1.0.0"),
            messages_api=stub,
            browser=browser,
            catalog_root=catalog_root,
            evidence_root=tmp_path / "evidence",
            **kw,
        )
        return result, stub, catalog_root

    return go


def evidence_text(folder: Path) -> str:
    return "\n".join(
        p.read_text(errors="ignore") for p in folder.rglob("*") if p.suffix in {".json", ".jsonl", ".html"}
    )


async def test_discover_compile_selftest_save(run_pipeline):
    result, _, root = await run_pipeline(HAPPY_PATH)
    assert result.status == "saved", result.reason
    assert result.artifact_path == catalog.path_for(CAP_ID, "1.0.0", root)
    saved = catalog.load(CAP_ID, root=root)
    assert saved.review.status.value == "draft"
    assert [s.action.type for s in saved.steps] == ["navigate", "click", "fill", "click", "extract"]

    evidence = result.evidence_dir
    assert evidence is not None
    for name in ("trace.json", "artifact.json", "selftest.json", "run.jsonl"):
        assert (evidence / name).exists(), name
    replays = list(evidence.glob("replay_*"))
    assert len(replays) == 1 and (replays[0] / "result.json").exists()  # the self-test's own evidence
    text = evidence_text(evidence)
    for raw in RAW:
        assert raw not in text, raw


async def test_existing_version_is_refused_before_any_model_call(run_pipeline):
    first, _, _ = await run_pipeline(HAPPY_PATH)
    assert first.status == "saved"
    again, stub, _ = await run_pipeline(HAPPY_PATH)
    assert again.status == "refused" and "already exists" in again.reason
    assert stub.requests == []


async def test_malformed_capability_id_is_refused(run_pipeline):
    result, stub, _ = await run_pipeline(HAPPY_PATH, capability_id="LookupBalance")
    assert result.status == "refused" and stub.requests == []


async def test_escalated_discovery_is_not_compiled(run_pipeline):
    result, _, root = await run_pipeline([call("ask_human", reason="lost")])
    assert result.status == "discovery_failed" and "escalated" in result.reason
    assert result.evidence_dir is not None and not (result.evidence_dir / "artifact.json").exists()
    assert not root.exists()


async def test_nothing_to_compile_is_a_compile_failure(run_pipeline):
    result, _, _ = await run_pipeline([call("done", summary="nothing to do")])
    assert result.status == "compile_failed" and "no successful actions" in result.reason


async def test_failed_selftest_keeps_the_draft_out_of_the_catalog(run_pipeline, monkeypatch):
    real = pipeline.compile_trace

    def broken(*args, **kwargs) -> CapabilityArtifact:
        """A checkpoint the live app never shows: the self-test replay must not pass."""
        art = real(*args, **kwargs)
        data = art.model_dump(mode="json")
        step = next(s for s in data["steps"] if s.get("expect"))
        step["expect"]["all_of"].append({"kind": "text_present", "text": "Screen That Never Appears"})
        return CapabilityArtifact.model_validate(data)

    monkeypatch.setattr(pipeline, "compile_trace", broken)
    result, _, root = await run_pipeline(HAPPY_PATH)
    assert result.status == "selftest_failed" and "UNKNOWN_STATE" in result.reason
    assert result.evidence_dir is not None and result.artifact_path == result.evidence_dir / "artifact.json"
    assert not catalog.path_for(CAP_ID, "1.0.0", root).exists()


# ---- the compiled artifact through the fault matrix --------------------------------------------- #

CASES = [
    ("10002", None, "success", "SUCCESS"),
    ("99999", None, "business_outcome", "MEMBER_NOT_FOUND"),
    ("10003", None, "business_outcome", "NO_SHARE_SAVINGS"),  # derived name (fixture: NO_SAVINGS_ACCOUNT)
    ("10001", "not_found", "business_outcome", "MEMBER_NOT_FOUND"),
    ("10001", "notice", "success", "SUCCESS"),
    ("10001", "slow", "success", "SUCCESS"),
    ("10001", "session_expired", "success", "SUCCESS"),
    ("10001", "denied", "failed", "PERMISSION_DENIED"),
    ("10001", "error", "failed", "APP_ERROR"),
    ("10001", "maint", "failed", "UNKNOWN_STATE"),
]


@pytest.fixture(scope="module")
def compiled() -> CapabilityArtifact:
    from cua.config import load_policy, load_tenant

    trace = DiscoveryTrace.model_validate_json(FIXTURE_TRACE.read_text())
    return compile_trace(trace, CAP_ID, tenant=load_tenant("cu_alpha"), policy=load_policy())


@pytest.mark.parametrize("member_id,fault,status,code", CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
async def test_compiled_artifact_fault_matrix(
    compiled, tenant, test_policy, browser, tmp_path, member_id, fault, status, code
):
    result = await execute(
        compiled,
        tenant,
        test_policy,
        {"member_id": member_id},
        mode="supervised",
        fault=fault,
        browser=browser,
        evidence_root=tmp_path,
        handoff_timeout_s=0.1,
    )
    assert result.status.value == status, (result.outcome_code, result.failure)
    if status == "failed":
        assert result.failure is not None and result.failure.category.value == code
    else:
        assert result.outcome_code == code
    if member_id == "10002" and fault is None:
        assert result.outputs == {"savings_balance": "1203.55", "account_status": "Active"}


def test_compiled_checkpoints_name_no_member_data(compiled):
    texts = [
        p.text
        for s in compiled.steps
        if s.expect
        for p in s.expect.predicates()
        if isinstance(p, TextPresent)
    ]
    assert texts == ["Member Summary"]
