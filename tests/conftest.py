from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.config import load_policy
from cua.schema.artifact import CapabilityArtifact

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "capabilities/mockbank/member.lookup_savings_balance/1.0.0.json"


@pytest.fixture
def policy():
    return load_policy(ROOT / "config/policy.yaml")


@pytest.fixture
def raw_artifact() -> dict:
    return json.loads(ARTIFACT_PATH.read_text())


@pytest.fixture
def artifact(raw_artifact) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(raw_artifact)


@pytest.fixture
def approved_artifact(raw_artifact) -> CapabilityArtifact:
    raw_artifact["review"] = {"status": "approved", "reviewed_by": "tester"}
    return CapabilityArtifact.model_validate(raw_artifact)
