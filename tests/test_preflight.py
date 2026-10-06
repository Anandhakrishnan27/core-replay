from cua.replay.preflight import PreflightOk, preflight
from cua.schema.artifact import CapabilityArtifact, FailureCategory


def test_valid_inputs_pass(approved_artifact, policy):
    assert isinstance(preflight(approved_artifact, {"member_id": "10001"}, policy), PreflightOk)


def test_bad_pattern_rejected_without_echoing_value(approved_artifact, policy):
    f = preflight(approved_artifact, {"member_id": "abc"}, policy)
    assert f.category is FailureCategory.INPUT_INVALID
    assert "abc" not in f.observed


def test_missing_and_unknown_inputs(approved_artifact, policy):
    assert preflight(approved_artifact, {}, policy).category is FailureCategory.INPUT_INVALID
    f = preflight(approved_artifact, {"member_id": "10001", "x": "1"}, policy)
    assert f.category is FailureCategory.INPUT_INVALID


def test_draft_rejected_unattended_but_allowed_supervised(artifact, policy):
    assert preflight(artifact, {"member_id": "10001"}, policy).category is FailureCategory.POLICY_VIOLATION
    assert isinstance(preflight(artifact, {"member_id": "10001"}, policy, mode="supervised"), PreflightOk)


def test_irreversible_needs_confirmation(raw_artifact, policy):
    raw_artifact["steps"][3]["risk"] = "irreversible"
    raw_artifact["policy"] = {"max_risk": "irreversible", "requires_confirmation": True}
    raw_artifact["review"] = {"status": "approved", "reviewed_by": "t"}
    art = CapabilityArtifact.model_validate(raw_artifact)
    assert preflight(art, {"member_id": "10001"}, policy).category is FailureCategory.POLICY_VIOLATION
    assert isinstance(preflight(art, {"member_id": "10001"}, policy, confirmed=True), PreflightOk)
