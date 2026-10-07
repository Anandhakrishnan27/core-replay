"""CLI wiring for the operator: when it is attached, headed vs headless, the printed tokened URL, busy port.

The replay / discovery functions are replaced by fakes that record what the CLI passed them and, while
"running", check that the operator page really serves (with its token) — no browser, no mock bank.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

import cua.discovery.agent as agent
import cua.discovery.pipeline as pipeline
import cua.replay.executor as executor
from cua.cli import app
from cua.schema.artifact import FailureCategory
from cua.schema.result import Failure, RunResult, RunStatus

CAP = "mockbank.member.lookup_savings_balance"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _page_serves(op: Any) -> dict[str, int]:
    async with httpx2.AsyncClient() as http:
        return {
            "with_token": (await http.get(op.url)).status_code,
            "without": (await http.get(f"http://127.0.0.1:{op.port}/api/interventions")).status_code,
        }


@pytest.fixture
def fake_replay(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake(capability_id: str, tenant: str, inputs: dict[str, str], **kw: Any) -> RunResult:
        seen.update(kw)
        seen["page"] = await _page_serves(kw["operator"]) if kw["operator"] is not None else None
        return RunResult(
            run_id="r1",
            mode="replay",
            capability_id=capability_id,
            capability_version="1.0.0",
            tenant_id=tenant,
            status=RunStatus.rejected,
            failure=Failure(
                category=FailureCategory.INPUT_INVALID, expected="digits", observed="x", retryable=False
            ),
            started_at=datetime.now(UTC),
            duration_ms=1,
            evidence_dir="evidence/_scratch/r1",
        )

    monkeypatch.setattr(executor, "replay", fake)
    return seen


def replay(*args: str, port: int | None = None) -> Any:
    port = port or _free_port()
    return CliRunner().invoke(
        app,
        ["replay", CAP, "-i", "member_id=1", "--operator-port", str(port), *args],
        env={"CUA_HEADLESS": ""},
    )


@pytest.mark.parametrize(
    "args,attached,headed",
    [
        ([], False, False),  # unattended: no operator, escalation ends at once
        (["--supervised"], True, True),  # the human uses the run's own (headed) window
        (["--supervised", "--headless"], True, False),
        (["--operator"], True, True),  # explicit opt-in for an unattended run
        (["--supervised", "--no-operator"], False, False),
    ],
)
def test_replay_attaches_the_operator_only_when_asked(fake_replay, args, attached, headed):
    result = replay(*args)
    assert result.exit_code == 2, result.output  # the fake says rejected
    assert (fake_replay["operator"] is not None) is attached
    assert fake_replay["headed"] is headed
    assert "handoff_timeout_s" not in fake_replay  # the policy's timeout (900 s) applies
    if attached:
        assert fake_replay["page"] == {"with_token": 200, "without": 403}
        assert "operator page: http://127.0.0.1:" in result.stderr and "?token=" in result.stderr
        token = result.stderr.split("?token=", 1)[1].split()[0]
        assert token not in result.stdout  # the RunResult JSON (and so evidence) never holds it
    else:
        assert "operator page" not in result.stderr


def test_replay_evidence_goes_to_scratch_unless_a_folder_is_given(fake_replay, tmp_path):
    assert replay("--no-operator").exit_code == 2
    assert fake_replay["evidence_root"] == executor.SCRATCH_RUNS
    assert replay("--no-operator", "--evidence-dir", str(tmp_path)).exit_code == 2
    assert fake_replay["evidence_root"] == tmp_path


def test_busy_operator_port_is_refused_before_the_run(fake_replay):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        result = replay("--supervised", port=busy.getsockname()[1])
    assert result.exit_code == 2
    assert "in use" in result.stderr
    assert fake_replay == {}  # never started


@pytest.mark.parametrize("args,attached", [([], True), (["--no-operator"], False)])
def test_discover_attaches_the_operator_by_default(monkeypatch, args, attached):
    seen: dict[str, Any] = {}

    async def fake(*a: Any, **kw: Any) -> pipeline.Discovered:
        seen.update(kw)
        seen["page"] = await _page_serves(kw["operator"]) if kw["operator"] is not None else None
        return pipeline.Discovered("refused", "fake", Path("x"))

    monkeypatch.setattr(pipeline, "discover_capability", fake)
    monkeypatch.setattr(agent, "default_messages_api", lambda: object())
    result = CliRunner().invoke(
        app,
        ["discover", "--goal", "g", "--capability-id", CAP, "-p", "member_id=1"]
        + ["--operator-port", str(_free_port()), *args],
        env={"CUA_HEADLESS": ""},
    )
    assert result.exit_code == 2, result.output
    assert (seen["operator"] is not None) is attached
    assert seen["evidence_root"] == agent.SCRATCH_RUNS  # git-ignored unless --evidence-dir is given
    if attached:
        assert seen["page"] == {"with_token": 200, "without": 403}


def test_cli_sets_no_default_credentials(monkeypatch, tmp_path):
    """Credentials come only from env / .env: importing the CLI must not invent any."""
    import importlib

    import cua.cli

    monkeypatch.chdir(tmp_path)  # no .env here for load_dotenv to find
    monkeypatch.delenv("MOCKBANK_USER", raising=False)
    monkeypatch.delenv("MOCKBANK_PASSWORD", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    importlib.reload(cua.cli)
    import os

    assert "MOCKBANK_USER" not in os.environ and "MOCKBANK_PASSWORD" not in os.environ
