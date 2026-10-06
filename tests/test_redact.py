from cua.safety.redact import hash_value, redact, redact_mapping, redact_typed
from cua.schema.artifact import Sensitivity


def test_pii_is_hashed_and_stable():
    a, b = redact("10001", Sensitivity.pii, "member_id"), redact("10001", Sensitivity.pii, "member_id")
    assert a == b and "10001" not in a and a.startswith("«member_id:sha256:")


def test_secret_never_appears():
    assert redact("hunter2", Sensitivity.secret, "mockbank_password") == "«secret:mockbank_password»"


def test_internal_and_public_kept():
    assert redact("Active", Sensitivity.internal) == "Active"
    assert redact("x", Sensitivity.public) == "x"


def test_typed_values_reduced_to_length():
    assert redact_typed("10001") == "«redacted:len=5»"


def test_mapping():
    out = redact_mapping(
        {"savings_balance": "2450.17", "account_status": "Active"}, {"savings_balance": Sensitivity.pii}
    )
    assert out["account_status"] == "Active" and "2450.17" not in out["savings_balance"]


def test_different_values_different_hashes():
    assert hash_value("10001", "m") != hash_value("10002", "m")
