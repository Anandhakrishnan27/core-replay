"""Minimal operator API + page (served in-process on :8001 during a run). Phase 5.

Runs in the SAME process/event loop as the executor, so take-control / hand-back act on the live
SessionControl and the live browser. A real system would stream the browser (CDP screencast / noVNC):
here the operator uses the headed Chromium window directly (documented as mocked).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from cua.handoff.controller import InvalidTransition, SessionControl

app = FastAPI(title="CUA Operator")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# run_id -> live control (registered by the executor / discovery agent when a run starts)
SESSIONS: dict[str, SessionControl] = {}


def _get(run_id: str) -> SessionControl:
    if run_id not in SESSIONS:
        raise HTTPException(404, "unknown run")
    return SESSIONS[run_id]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "operator.html", {"sessions": SESSIONS})


@app.get("/api/interventions")
async def interventions() -> list[dict[str, object]]:
    return [
        {
            "run_id": rid,
            "state": c.state.value,
            "holder": c.holder,
            "request": c.request.model_dump(mode="json") if c.request else None,
        }
        for rid, c in SESSIONS.items()
    ]


def _transition(run_id: str, op: str, operator_id: str | None = None) -> dict[str, str]:
    c = _get(run_id)
    try:
        if op == "take":
            c.take_control(operator_id or "operator")
        elif op == "back":
            c.hand_back()
        else:
            c.abort()
    except InvalidTransition as e:
        raise HTTPException(409, str(e)) from e
    return {"state": c.state.value, "holder": c.holder}


@app.post("/api/runs/{run_id}/take-control")
async def take_control(run_id: str, operator_id: str = "operator") -> dict[str, str]:
    return _transition(run_id, "take", operator_id)


@app.post("/api/runs/{run_id}/hand-back")
async def hand_back(run_id: str) -> dict[str, str]:
    return _transition(run_id, "back")


@app.post("/api/runs/{run_id}/abort")
async def abort(run_id: str) -> dict[str, str]:
    return _transition(run_id, "abort")
