"""Operator API + page, served in-process on 127.0.0.1 during a supervised run.

Runs in the SAME process and event loop as the executor / discovery agent, so take-control, hand-back
and abort act directly on the live SessionControl, and the human drives the live (headed) browser.
A real deployment would stream that browser (CDP screencast / noVNC); here the operator uses the
headed Chromium window directly (documented as mocked).

Access: bound to 127.0.0.1 only, plus a random token per server (one server per run), printed in the
operator URL. Every route requires it (`?token=` or `X-Operator-Token`), so another web page open in
the operator's browser cannot drive the API (CSRF). Real authentication is listed under Cuts.

    async with operator_server(port=8001) as op:
        entry = op.register(run_id, control, evidence_dir, mode="replay")
        ...                              # run; escalations wait on control
        op.unregister(run_id)
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from cua.handoff.controller import InvalidTransition, SessionControl
from cua.schema.result import HumanAction
from cua.surface.wait import poll_until

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
TOKEN_HEADER = "X-Operator-Token"
_OPERATOR_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class OperatorUnavailable(Exception):
    """The operator server could not start (e.g. the port is in use)."""


@dataclass
class RunEntry:
    run_id: str
    control: SessionControl
    evidence_dir: Path
    mode: Literal["discovery", "replay"]
    human_actions: list[HumanAction] = field(default_factory=list)  # filled by the recorder (redacted)


@dataclass
class OperatorServer:
    host: str
    port: int
    token: str
    runs: dict[str, RunEntry] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def register(
        self, run_id: str, control: SessionControl, evidence_dir: Path, mode: Literal["discovery", "replay"]
    ) -> RunEntry:
        entry = RunEntry(run_id=run_id, control=control, evidence_dir=evidence_dir, mode=mode)
        self.runs[run_id] = entry
        return entry

    def unregister(self, run_id: str) -> None:
        self.runs.pop(run_id, None)


def create_app(server: OperatorServer) -> FastAPI:
    app = FastAPI(title="CUA Operator", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
        given = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
        if not secrets.compare_digest(given, server.token):
            return JSONResponse({"detail": "missing or wrong operator token"}, status_code=403)
        return await call_next(request)

    def entry_for(run_id: str) -> RunEntry:
        if run_id not in server.runs:
            raise HTTPException(404, "unknown run")
        return server.runs[run_id]

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "operator.html", {})

    @app.get("/api/interventions")
    async def interventions() -> list[dict[str, object]]:
        return [
            {
                "run_id": e.run_id,
                "mode": e.mode,
                "state": e.control.state.value,
                "holder": e.control.holder,
                "epoch": e.control.epoch,
                "request": e.control.request.model_dump(mode="json") if e.control.request else None,
                "human_actions": len(e.human_actions),
            }
            for e in server.runs.values()
        ]

    @app.get("/api/runs/{run_id}/actions")
    async def actions(run_id: str) -> list[dict[str, object]]:
        return [a.model_dump(mode="json") for a in entry_for(run_id).human_actions]

    @app.get("/api/runs/{run_id}/screenshot")
    async def screenshot(run_id: str) -> FileResponse:
        """The CURRENT request's masked screenshot. No path comes from the client."""
        e = entry_for(run_id)
        rel = e.control.request.screenshot if e.control.request else None
        if not rel:
            raise HTTPException(404, "no screenshot for this run")
        base = e.evidence_dir.resolve()
        path = (base / rel).resolve()
        if not path.is_relative_to(base) or path.suffix != ".png" or not path.is_file():
            raise HTTPException(404, "no screenshot for this run")
        return FileResponse(path, media_type="image/png")

    async def transition(run_id: str, op: str, operator_id: str = "operator") -> dict[str, str]:
        c = entry_for(run_id).control
        try:
            if op == "take":
                if not _OPERATOR_ID_RE.match(operator_id):
                    raise HTTPException(422, "operator_id: letters, digits and . _ @ - only (max 64)")
                c.take_control(operator_id)
            elif op == "back":
                c.hand_back()
            else:
                c.abort()
        except InvalidTransition as e:
            raise HTTPException(409, str(e)) from e
        await c.flush()  # e.g. the live page is unlocked before "take control" returns
        return {"state": c.state.value, "holder": c.holder}

    @app.post("/api/runs/{run_id}/take-control")
    async def take_control(run_id: str, operator_id: str = "operator") -> dict[str, str]:
        return await transition(run_id, "take", operator_id)

    @app.post("/api/runs/{run_id}/hand-back")
    async def hand_back(run_id: str) -> dict[str, str]:
        return await transition(run_id, "back")

    @app.post("/api/runs/{run_id}/abort")
    async def abort(run_id: str) -> dict[str, str]:
        return await transition(run_id, "abort")

    return app


@asynccontextmanager
async def operator_server(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, token: str | None = None
) -> AsyncIterator[OperatorServer]:
    """Start the operator API as a task in the CURRENT event loop; stop it on exit. Fails fast."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OperatorUnavailable("the operator page is served on loopback only (no authentication)")
    server = OperatorServer(host=host, port=port, token=token or secrets.token_urlsafe(18))
    uv = uvicorn.Server(
        uvicorn.Config(create_app(server), host=host, port=port, log_level="warning", lifespan="off")
    )

    async def serve() -> None:
        try:
            await uv.serve()
        except SystemExit:
            # uvicorn calls sys.exit() when it cannot bind. Caught HERE: a SystemExit leaving a task would
            # propagate out of the whole event loop and take the run down with it.
            pass

    task = asyncio.create_task(serve())

    async def up() -> bool:
        return uv.started or task.done()

    await poll_until(up, timeout_ms=5_000, interval_ms=20)
    if not uv.started:
        uv.should_exit = True
        await task
        raise OperatorUnavailable(f"could not start the operator page on {host}:{port} (port in use?)")
    try:
        yield server
    finally:
        uv.should_exit = True
        await task
