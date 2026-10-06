import pytest

from cua.safety.policy import NeedsHuman, PolicyGate, PolicyViolation
from cua.schema.artifact import RiskClass


def test_origin_allowlist(policy):
    gate = PolicyGate(policy)
    assert gate.origin_allowed("http://localhost:8000/console/mbrlookup")
    assert not gate.origin_allowed("https://evil.example.com/steal")
    assert not gate.origin_allowed("http://localhost:9999/")


def test_disallowed_action_type(policy):
    with pytest.raises(PolicyViolation):
        PolicyGate(policy).authorize(action_type="upload_file", risk=RiskClass.read, mode="replay")


def test_navigation_outside_allowlist_blocked(policy):
    with pytest.raises(PolicyViolation):
        PolicyGate(policy).authorize(
            action_type="navigate", risk=RiskClass.read, mode="replay", url="https://other-bank.example"
        )


def test_irreversible_in_discovery_needs_human(policy):
    with pytest.raises(NeedsHuman):
        PolicyGate(policy).authorize(action_type="click", risk=RiskClass.irreversible, mode="discovery")


def test_irreversible_in_replay_needs_confirmation(policy):
    gate = PolicyGate(policy)
    with pytest.raises(PolicyViolation):
        gate.authorize(action_type="click", risk=RiskClass.irreversible, mode="replay")
    gate.authorize(action_type="click", risk=RiskClass.irreversible, mode="replay", confirmed=True)


def test_irreversible_name_detection(policy):
    gate = PolicyGate(policy)
    assert gate.is_irreversible_name("Open Account")
    assert gate.is_irreversible_name("CONFIRM TRANSFER")
    assert not gate.is_irreversible_name("Search")
