import pytest
import yaml

from cua.replay.overrides import apply_overrides
from tests.conftest import ROOT


def test_tenant_override_patches_targets_only(artifact):
    patch = yaml.safe_load((ROOT / "config/tenants/cu_beta.yaml").read_text())["overrides"][artifact.id]
    effective, digest = apply_overrides(artifact, patch)
    assert effective.targets["member_id_field"].locators[0].label == "Account Holder #"
    assert effective.contract == artifact.contract
    _, base_digest = apply_overrides(artifact, None)
    assert digest != base_digest


def test_override_cannot_change_contract(artifact):
    with pytest.raises(ValueError, match="may only patch"):
        apply_overrides(artifact, {"contract": {"inputs": []}})
