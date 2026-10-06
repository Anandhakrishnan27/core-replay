"""Redaction applied to everything written to logs/evidence. Artifacts hold templates, never values."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from cua.schema.artifact import Sensitivity

_DEFAULT_SALT = "dev-only-salt"


def _salt() -> str:
    return os.getenv("CUA_REDACTION_SALT", _DEFAULT_SALT)


def hash_value(value: str, label: str) -> str:
    digest = hashlib.sha256((_salt() + value).encode()).hexdigest()[:10]
    return f"«{label}:sha256:{digest}»"


def redact(value: Any, sensitivity: Sensitivity, label: str = "value") -> Any:
    """Return a log-safe representation of `value`."""
    if value is None:
        return None
    if sensitivity is Sensitivity.secret:
        return f"«secret:{label}»"
    if sensitivity is Sensitivity.pii:
        return hash_value(str(value), label)
    return value


def redact_typed(value: str) -> str:
    """For human-typed values captured during handoff: keep only the length."""
    return f"«redacted:len={len(value)}»"


def redact_mapping(data: dict[str, Any], sensitivity_by_key: dict[str, Sensitivity]) -> dict[str, Any]:
    """Redact known keys; unknown keys default to `internal` (kept)."""
    return {k: redact(v, sensitivity_by_key.get(k, Sensitivity.internal), k) for k, v in data.items()}
