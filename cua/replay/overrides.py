"""Tenant overrides: base artifact + per-tenant patch = effective artifact (hash logged per run).

Patches may only touch `targets` and `conditions` (implementation), never `contract` (the API).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cua.schema.artifact import CapabilityArtifact

_ALLOWED_KEYS = {"targets", "conditions"}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def apply_overrides(
    artifact: CapabilityArtifact, patch: dict[str, Any] | None
) -> tuple[CapabilityArtifact, str]:
    base = artifact.model_dump(mode="json")
    if patch:
        illegal = set(patch) - _ALLOWED_KEYS
        if illegal:
            raise ValueError(
                f"tenant overrides may only patch {sorted(_ALLOWED_KEYS)}, got {sorted(illegal)}"
            )
        base = _deep_merge(base, patch)
    effective = CapabilityArtifact.model_validate(base)
    digest = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()[:12]
    return effective, digest
