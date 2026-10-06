"""Deterministic replay engine. Phase 3. No LLM imports allowed in this package.

async def replay(capability_id, version, tenant_id, inputs, *, mode, confirmed, fault=None) -> RunResult:
    artifact = load + validate
    effective, digest = apply_overrides(artifact, tenant.overrides.get(capability_id))
    pre = preflight(effective, inputs, policy, mode=mode, confirmed=confirmed)  → rejected on Failure
    start browser context (headed if handoff possible) → SessionProvider.login
    check app.fingerprint                                                   → APP_VERSION_MISMATCH
    for step in steps:
        await control.ensure_automation()           # handoff seam
        gate.authorize(...)                         # policy
        resolved = resolve(target)                  # ranked locators + MatchRule
        await surface.act(...)
        outcome = await race(step)                  # conditions → checkpoint → UNKNOWN_STATE
        extract → parse_value → type-check vs OutputSpec
        logger.event(...) + screenshot
    check artifact.success → RunResult(success, outputs)
On escalate: control.request_intervention(...) → await human → resync to furthest satisfied checkpoint.
"""

from __future__ import annotations

from cua.schema.result import RunResult


async def replay(
    capability_id: str,
    tenant_id: str,
    inputs: dict[str, str],
    *,
    version: str | None = None,
    mode: str = "unattended",
    confirmed: bool = False,
    fault: str | None = None,
) -> RunResult:
    raise NotImplementedError("phase-3: replay executor")
