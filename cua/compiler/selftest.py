"""Compiler self-test: replay the compiled draft once, with the discovery inputs, before it is saved.

Runs the real executor (cua.replay.executor.execute) in supervised mode, which means:
  - a NEW browser context and a fresh SessionProvider login: nothing (cookies, session, open dialogs)
    carries over from the discovery run, so the artifact cannot pass on leftover state
  - the same pre-flight as a catalog replay (inputs, template resolution, app version)
  - no operator: an escalation ends the self-test at once (handoff resolution aborted)
It passes only if
  1. the run ends `success`
  2. every output equals the value discovery read (compared in memory; only true/false is recorded)
  3. every target resolved with its FIRST locator (no drift on the very page it was compiled from)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from playwright.async_api import Browser

from cua.config import ROOT, Policy, Tenant
from cua.replay.executor import execute
from cua.replay.extract import ParseError, parse_value
from cua.schema.artifact import CapabilityArtifact, Extract
from cua.schema.result import RunResult, RunStatus, Scalar


@dataclass
class SelfTest:
    passed: bool
    reason: str  # log-safe
    run: RunResult
    drifted_targets: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """Evidence-safe: statuses, codes and target ids only, never values."""
        return {
            "passed": self.passed,
            "reason": self.reason,
            "replay_run_id": self.run.run_id,
            "status": self.run.status.value,
            "outcome_code": self.run.outcome_code,
            "failure_category": self.run.failure.category.value if self.run.failure else None,
            "drifted_targets": self.drifted_targets,
            "evidence_dir": self.run.evidence_dir,
        }


def _canonical(value: str | int | Decimal | date) -> Scalar:
    """Same normalisation the executor applies to outputs (money as an exact string, dates ISO)."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def expected_outputs(artifact: CapabilityArtifact, raw: dict[str, str]) -> dict[str, Scalar] | None:
    """Discovery's raw on-screen texts, parsed exactly as replay parses them. None if one can't parse."""
    parses = {s.action.output: s.action.parse for s in artifact.steps if isinstance(s.action, Extract)}
    try:
        return {name: _canonical(parse_value(raw[name], parses[name])) for name in parses if name in raw}
    except ParseError:
        return None  # never forward the message: it quotes the value


def _drift(run: RunResult) -> list[str]:
    evidence = Path(run.evidence_dir)
    log = (evidence if evidence.is_absolute() else ROOT / evidence) / "run.jsonl"
    if not log.exists():
        return []
    drifted = []
    for line in log.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        details = event.get("details", {})
        if event.get("event") == "target_resolved" and details.get("locator_index", 0) > 0:
            drifted.append(str(details.get("target")))
    return sorted(set(drifted))


async def self_test(
    artifact: CapabilityArtifact,
    tenant: Tenant,
    policy: Policy,
    inputs: dict[str, str],
    discovered_outputs: dict[str, str],
    *,
    evidence_root: Path,
    browser: Browser | None = None,
) -> SelfTest:
    run = await execute(
        artifact,
        tenant,
        policy,
        inputs,
        mode="supervised",
        browser=browser,  # a browser PROCESS may be shared; execute() always opens a new context + login
        evidence_root=evidence_root,  # no operator: an escalation ends the self-test at once
    )
    if run.status is not RunStatus.success:
        detail = run.outcome_code or (run.failure.category.value if run.failure else "")
        step = f" at step '{run.failure.step_id}'" if run.failure and run.failure.step_id else ""
        return SelfTest(False, f"replay ended {run.status.value} {detail}{step}".strip(), run)
    expected = expected_outputs(artifact, discovered_outputs)
    if expected is None or run.outputs != expected:
        return SelfTest(False, "replay outputs differ from the values read during discovery", run)
    drifted = _drift(run)
    if drifted:
        return SelfTest(False, f"targets resolved by a fallback locator: {drifted}", run, drifted)
    return SelfTest(True, "replay succeeded with the discovery inputs; outputs match; no locator drift", run)
