"""Operator API: token on every route, live transitions on SessionControl, screenshot guard, lifecycle."""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from cua.handoff.controller import ControlState, HandoffAborted, SessionControl
from cua.handoff.models import InterventionRequest
from cua.handoff.operator import OperatorServer, OperatorUnavailable, create_app, operator_server

TOKEN = "t0k3n-for-tests"


def request(screenshot: str | None = "steps/01_x.png") -> InterventionRequest:
    return InterventionRequest(
        intervention_id="int_1",
        run_id="r1",
        mode="replay",
        capability_id="mockbank.member.lookup_savings_balance",
        step_id="click_search_button",
        reason="screen matches neither the checkpoint nor any known condition",
        category="UNKNOWN_STATE",
        current_url="http://127.0.0.1:8000/console",
        expected_state="text_present(Member Summary)",
        screenshot=screenshot,
        requested_at=datetime.now(UTC),
    )


@pytest.fixture
def setup(tmp_path):
    server = OperatorServer(host="127.0.0.1", port=0, token=TOKEN)
    control = SessionControl("r1")
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "01_x.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "secret.png").write_bytes(b"\x89PNG outside steps is fine but must be the request's own")
    server.register("r1", control, tmp_path, mode="replay")
    client = TestClient(create_app(server), headers={"X-Operator-Token": TOKEN})
    return server, control, client


def pause(control: SessionControl, req: InterventionRequest) -> None:
    """Put the control in PAUSED as an escalation would (without awaiting the hand-back)."""
    control.state, control.request = ControlState.PAUSED, req


# ---- token -------------------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,method",
    [
        ("/", "get"),
        ("/api/interventions", "get"),
        ("/api/runs/r1/actions", "get"),
        ("/api/runs/r1/screenshot", "get"),
        ("/api/runs/r1/take-control", "post"),
        ("/api/runs/r1/hand-back", "post"),
        ("/api/runs/r1/abort", "post"),
    ],
)
def test_every_route_requires_the_token(setup, path, method):
    server, _, _ = setup
    bare = TestClient(create_app(server))
    assert getattr(bare, method)(path).status_code == 403
    assert getattr(bare, method)(path, headers={"X-Operator-Token": "wrong"}).status_code == 403


def test_token_in_query_string_works_for_the_page(setup):
    server, _, _ = setup
    r = TestClient(create_app(server)).get(f"/?token={TOKEN}")
    assert r.status_code == 200 and "CUA Operator" in r.text


# ---- transitions -------------------------------------------------------------------------------- #


def test_take_control_and_hand_back_drive_the_live_control(setup):
    _, control, client = setup
    pause(control, request())
    [run] = client.get("/api/interventions").json()
    assert run["state"] == "PAUSED" and run["request"]["step_id"] == "click_search_button"

    r = client.post("/api/runs/r1/take-control", params={"operator_id": "ana@desk-1"})
    assert r.json() == {"state": "HUMAN", "holder": "human:ana@desk-1"}
    assert client.post("/api/runs/r1/take-control").status_code == 409  # already held
    assert client.post("/api/runs/r1/hand-back").json()["state"] == "RESUMING"


def test_abort_and_invalid_transitions(setup):
    _, control, client = setup
    assert client.post("/api/runs/r1/hand-back").status_code == 409  # automation holds control
    pause(control, request())
    assert client.post("/api/runs/r1/abort").json()["state"] == "ABORTED"


def test_operator_id_is_validated(setup):
    _, control, client = setup
    pause(control, request())
    assert client.post("/api/runs/r1/take-control", params={"operator_id": "<script>"}).status_code == 422
    assert control.state is ControlState.PAUSED


def test_unknown_run_is_404(setup):
    _, _, client = setup
    assert client.post("/api/runs/nope/take-control").status_code == 404


async def test_hand_back_through_the_api_releases_a_waiting_escalation(setup):
    _, control, client = setup
    waiting = asyncio.create_task(control.request_intervention(request(), timeout_s=5))
    await asyncio.sleep(0)  # let it enter PAUSED
    client.post("/api/runs/r1/take-control")
    client.post("/api/runs/r1/hand-back")
    await asyncio.wait_for(waiting, 1)
    assert control.state is ControlState.RESUMING


async def test_abort_through_the_api_ends_a_waiting_escalation(setup):
    _, control, client = setup
    waiting = asyncio.create_task(control.request_intervention(request(), timeout_s=5))
    await asyncio.sleep(0)
    client.post("/api/runs/r1/abort")
    with pytest.raises(HandoffAborted):
        await asyncio.wait_for(waiting, 1)


# ---- screenshot --------------------------------------------------------------------------------- #


def test_screenshot_is_the_current_requests_own_file(setup):
    _, control, client = setup
    assert client.get("/api/runs/r1/screenshot").status_code == 404  # no request yet
    pause(control, request())
    r = client.get("/api/runs/r1/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/hosts", "steps/../../outside.png", "run.jsonl"])
def test_screenshot_path_cannot_escape_the_evidence_folder(setup, bad):
    _, control, client = setup
    pause(control, request(screenshot=bad))
    assert client.get("/api/runs/r1/screenshot").status_code == 404


# ---- server lifecycle --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_server_runs_in_this_loop_and_stops_cleanly():
    port = _free_port()
    async with operator_server(port=port) as op:
        assert op.url == f"http://127.0.0.1:{port}/?token={op.token}" and len(op.token) >= 20
        async with httpx2.AsyncClient() as http:
            assert (await http.get(f"http://127.0.0.1:{port}/api/interventions")).status_code == 403
            ok = await http.get(
                f"http://127.0.0.1:{port}/api/interventions", headers={"X-Operator-Token": op.token}
            )
            assert ok.status_code == 200 and ok.json() == []
    with socket.socket() as s:  # port released
        s.bind(("127.0.0.1", port))


async def test_busy_port_fails_fast():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(OperatorUnavailable, match="in use"):
            async with operator_server(port=port):
                pass


async def test_only_loopback_hosts_are_allowed():
    with pytest.raises(OperatorUnavailable, match="loopback"):
        async with operator_server(host="0.0.0.0", port=_free_port()):
            pass


def test_register_and_unregister(tmp_path: Path):
    server = OperatorServer(host="127.0.0.1", port=0, token=TOKEN)
    server.register("r9", SessionControl("r9"), tmp_path, mode="discovery")
    assert "r9" in server.runs
    server.unregister("r9")
    server.unregister("r9")  # idempotent
    assert server.runs == {}
