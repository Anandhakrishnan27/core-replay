"""`cua discover`: discovery → compile → self-test → catalog. The model is used in the first step only.

    refuse early      capability id malformed, or that version already in the catalog (before any LLM call)
    discover          agent loop; trace.json (redacted) in evidence/…/discovery_<run_id>/
    compile           draft artifact → artifact.json in the same evidence folder (always kept for review)
    self-test         replay the draft with the discovery inputs in a NEW context (fresh login) →
                      selftest.json + the replay's own evidence under the discovery folder
    save              only if the self-test passed: capabilities/<product>/<capability>/<semver>.json
Every step that stops the pipeline leaves its evidence behind and says why; nothing raw is written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from playwright.async_api import Browser

from cua import catalog
from cua.compiler import CompileError
from cua.compiler.compile import compile_trace
from cua.compiler.selftest import self_test
from cua.config import CAPABILITIES_DIR, ROOT, Policy, Tenant
from cua.discovery.agent import DEFAULT_MODEL, SCRATCH_RUNS, MessagesAPI, discover
from cua.evidence.logger import RunLogger
from cua.handoff.operator import OperatorServer

Status = Literal["saved", "refused", "discovery_failed", "compile_failed", "selftest_failed"]
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
ARTIFACT_FILE = "artifact.json"
SELFTEST_FILE = "selftest.json"


@dataclass
class Discovered:
    status: Status
    reason: str  # log-safe
    evidence_dir: Path | None = None
    artifact_path: Path | None = None  # the catalog file when saved, else the draft in evidence

    def summary(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence_dir": str(self.evidence_dir) if self.evidence_dir else None,
            "artifact": str(self.artifact_path) if self.artifact_path else None,
        }


async def discover_capability(
    goal: str,
    tenant: Tenant,
    policy: Policy,
    params: dict[str, str],
    capability_id: str,
    version: str = "1.0.0",
    *,
    messages_api: MessagesAPI,
    model: str = DEFAULT_MODEL,
    headed: bool = False,
    fault: str | None = None,
    browser: Browser | None = None,
    operator: OperatorServer | None = None,
    catalog_root: Path = CAPABILITIES_DIR,
    evidence_root: Path = SCRATCH_RUNS,
) -> Discovered:
    if not _CAPABILITY_ID.match(capability_id) or not _SEMVER.match(version):
        return Discovered("refused", "capability id must be <product>.<domain>.<verb_noun> and version x.y.z")
    target = catalog.path_for(capability_id, version, catalog_root)
    if target.exists():
        # Saved versions are immutable (approved ones especially): changes get a new version.
        return Discovered("refused", f"{capability_id}@{version} already exists; pass a new --version")

    run = await discover(
        goal,
        tenant,
        policy,
        params,
        messages_api=messages_api,
        model=model,
        headed=headed,
        fault=fault,
        browser=browser,
        operator=operator,  # the self-test below never gets one: it must pass without a human
        evidence_root=evidence_root,
    )
    evidence = run.evidence_dir
    logger = RunLogger(evidence.parent, run.trace.run_id, "discovery")  # same folder: appends to run.jsonl
    if run.trace.status != "completed":
        return Discovered("discovery_failed", f"discovery {run.trace.status}: {run.reason}", evidence)

    try:
        artifact = compile_trace(run.trace, capability_id, version, tenant=tenant, policy=policy)
    except CompileError as e:
        logger.event("compile_failed", reason=str(e))
        return Discovered("compile_failed", str(e), evidence)
    draft = logger.write_json(ARTIFACT_FILE, artifact)
    logger.event("compiled", capability=capability_id, version=version, steps=len(artifact.steps))

    result = await self_test(
        artifact,
        tenant,
        policy,
        dict(params),
        run.outputs,
        evidence_root=evidence,
        browser=browser,
    )
    (evidence / SELFTEST_FILE).write_text(json.dumps(result.summary(), indent=2), encoding="utf-8")
    logger.event("selftest", passed=result.passed, reason=result.reason, replay_run_id=result.run.run_id)
    if not result.passed:
        # Not saved to the catalog: the draft stays in evidence for review (artifact.json).
        return Discovered("selftest_failed", result.reason, evidence, draft)

    saved = catalog.save(artifact, catalog_root)
    logger.event("saved", path=str(saved.relative_to(ROOT)) if saved.is_relative_to(ROOT) else saved.name)
    return Discovered("saved", result.reason, evidence, saved)
