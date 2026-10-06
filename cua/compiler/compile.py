"""Trace → CapabilityArtifact. No LLM required. Phase 4.

Passes (each in its own module):
  1. clean          drop failed/undone/observation-only actions → shortest effective path
  2. parameterize   literals → {{inputs.x}} / {{secrets.x}} / {{tenant.base_url}}; infer types + patterns
  3. outputs        extract actions → OutputSpec + SUCCESS outcome returns
  4. locators       candidates from ElementSnapshot, verified unique against the recorded page, ranked
  5. checkpoints    page_before/after diff → stable postconditions (never data values)
  6. conditions     standard library + interstitials observed during the run (+ probe results)
  7. risk           per-step RiskClass; policy.max_risk; requires_confirmation
  8. validate       CapabilityArtifact.model_validate + self-test replay with the original inputs
Output: capabilities/<product>/<capability>/<semver>.json with review.status = draft.
"""

from __future__ import annotations

from cua.schema.artifact import CapabilityArtifact
from cua.schema.trace import DiscoveryTrace


def compile_trace(trace: DiscoveryTrace, capability_id: str, version: str = "1.0.0") -> CapabilityArtifact:
    raise NotImplementedError("phase-4: compiler pipeline")
