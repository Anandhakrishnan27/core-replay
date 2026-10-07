"""Schema tests: the example artifact is valid, and each design rule rejects a bad artifact."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.schema import CapabilityArtifact, RunResult

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "tests/fixtures/lookup_savings_balance.handwritten.json"
RESULTS = ROOT / "tests/fixtures/results.json"


@pytest.fixture
def raw() -> dict:
    return json.loads(ARTIFACT.read_text())


def _rejects(raw: dict, fragment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        CapabilityArtifact.model_validate(raw)
    assert fragment in str(exc.value), str(exc.value)


def test_example_artifact_is_valid(raw):
    art = CapabilityArtifact.model_validate(raw)
    assert art.contract.inputs[0].name == "member_id"


def test_example_results_are_valid():
    for r in json.loads(RESULTS.read_text()).values():
        RunResult.model_validate(r)


def test_fill_literal_rejected(raw):  # rule 3: no baked-in values / PII
    step = next(s for s in raw["steps"] if s["id"] == "enter_member_id")
    step["action"]["value"] = "10001"
    _rejects(raw, "must be a single template")


def test_undeclared_input_rejected(raw):
    step = next(s for s in raw["steps"] if s["id"] == "enter_member_id")
    step["action"]["value"] = "{{inputs.account_no}}"
    _rejects(raw, "undeclared input 'account_no'")


def test_unknown_target_rejected(raw):  # rule 1
    raw["steps"][1]["action"]["target"] = "nope"
    _rejects(raw, "unknown target 'nope'")


def test_css_only_target_rejected(raw):  # rule 2
    raw["targets"]["search_button"]["locators"] = [{"strategy": "css", "selector": "#btn"}]
    _rejects(raw, "needs at least one semantic locator")


def test_undeclared_outcome_rejected(raw):  # rule 4
    raw["conditions"]["member_not_found"]["handler"]["outcome"] = "NOT_THERE"
    _rejects(raw, "undeclared outcome 'NOT_THERE'")


def test_business_condition_cannot_fail(raw):  # rule 5
    raw["conditions"]["member_not_found"]["handler"] = {"do": "fail", "category": "APP_ERROR"}
    _rejects(raw, "cannot use handler 'fail'")


def test_irreversible_requires_confirmation(raw):  # rule 6
    raw["steps"][3]["risk"] = "irreversible"
    raw["policy"]["max_risk"] = "irreversible"
    _rejects(raw, "requires_confirmation must be true")


def test_policy_must_match_steps(raw):
    raw["policy"]["max_risk"] = "read"
    _rejects(raw, "riskiest step is 'reversible'")


def test_success_outputs_must_be_extracted(raw):
    raw["steps"] = [s for s in raw["steps"] if s["id"] != "read_status"]
    _rejects(raw, "never extracted")


def test_failed_result_needs_failure():
    r = copy.deepcopy(json.loads(RESULTS.read_text())["hard_failure"])
    del r["failure"]
    with pytest.raises(ValidationError):
        RunResult.model_validate(r)
