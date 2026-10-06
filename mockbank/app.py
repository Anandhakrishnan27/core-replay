"""MockBank Core (target app).

Legacy ON PURPOSE. Do not "clean up":
  - frameset: left `nav` frame + `main` frame
  - table-based layouts, no ids / data-testid, no <label for>
  - server-rendered HTML only, no API

Routes (all HTML):
  GET  /login, POST /login                 session cookie (credentials from env)
  GET  /console                            frameset (nav + main)
  GET  /console/nav                        navigation links
  GET  /console/home                       landing page ("MockBank Core v2.3")
  GET  /console/mbrlookup                  lookup form: "Member Number" + Search
  POST /console/mbrlookup                  → member summary | "No member found" | fault pages
  GET  /console/mbrlookup/result?t=        result for an opaque single-use ticket (slow refresh, maint retry)

Member numbers never appear in URLs: follow-up GETs carry a ticket, not the number.
Faults: see faults.py.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mockbank.data import find_member
from mockbank.faults import Fault, active_fault, apply_fault_param, mark_once, once_consumed

app = FastAPI(title="MockBank Core", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "mb_session"
SLOW_REFRESH_S = 2

# In-memory only: a restart signs everyone out, which is fine for a mock.
_sessions: set[str] = set()
_tickets: dict[str, str] = {}  # ticket -> member number


@app.middleware("http")
async def sticky_fault(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    apply_fault_param(request, response)
    return response


def _signed_on(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) in _sessions


def _to_login(expired: bool = False) -> RedirectResponse:
    return RedirectResponse("/login?expired=1" if expired else "/login", status_code=303)


async def _form(request: Request) -> dict[str, str]:
    """Parse an urlencoded body (avoids the python-multipart dependency)."""
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _new_ticket(member_id: str) -> str:
    ticket = secrets.token_urlsafe(12)
    _tickets[ticket] = member_id
    return ticket


def _result(request: Request, member_id: str) -> HTMLResponse:
    member = find_member(member_id)
    if member is None:
        return templates.TemplateResponse(request, "not_found.html")
    return templates.TemplateResponse(request, "member.html", {"member": member})


# --- sign-on -------------------------------------------------------------------------------------


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/console", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    expired = request.query_params.get("expired") == "1"
    return templates.TemplateResponse(request, "login.html", {"expired": expired, "invalid": False})


@app.post("/login", response_model=None)
async def login(request: Request) -> Response:
    form = await _form(request)
    user, password = os.getenv("MOCKBANK_USER"), os.getenv("MOCKBANK_PASSWORD")
    ok = (
        bool(user and password)
        and secrets.compare_digest(form.get("userid", "").encode(), (user or "").encode())
        and secrets.compare_digest(form.get("password", "").encode(), (password or "").encode())
    )
    if not ok:
        return templates.TemplateResponse(
            request, "login.html", {"expired": False, "invalid": True}, status_code=401
        )
    token = secrets.token_urlsafe(24)
    _sessions.add(token)
    response = RedirectResponse("/console", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


# --- console -------------------------------------------------------------------------------------


@app.get("/console", response_model=None)
async def console(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    return templates.TemplateResponse(request, "frameset.html")


@app.get("/console/nav", response_model=None)
async def nav(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    return templates.TemplateResponse(request, "nav.html")


@app.get("/console/home", response_model=None)
async def home(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    return templates.TemplateResponse(request, "home.html")


@app.get("/console/mbrlookup", response_model=None)
async def lookup_form(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    fault = active_fault(request)
    response: Response
    if fault is Fault.session_expired and not once_consumed(request, fault):
        _sessions.discard(request.cookies.get(SESSION_COOKIE, ""))
        response = _to_login(expired=True)
        mark_once(request, response, fault)
        return response
    if fault is Fault.notice and not once_consumed(request, fault):
        response = templates.TemplateResponse(request, "notice.html")
        mark_once(request, response, fault)
        return response
    return templates.TemplateResponse(request, "lookup.html")


@app.post("/console/mbrlookup", response_model=None)
async def search(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    member_id = (await _form(request)).get("mbrno", "").strip()
    fault = active_fault(request)
    response: Response
    if fault is Fault.denied:
        return templates.TemplateResponse(request, "denied.html", status_code=403)
    if fault is Fault.error:
        return templates.TemplateResponse(request, "error.html", status_code=500)
    if fault is Fault.maint and not once_consumed(request, fault):
        ticket = _new_ticket(member_id)
        response = templates.TemplateResponse(request, "maint.html", {"ticket": ticket}, status_code=503)
        mark_once(request, response, fault)
        return response
    if fault is Fault.slow:
        ticket = _new_ticket(member_id)
        return templates.TemplateResponse(
            request, "processing.html", {"ticket": ticket, "refresh_s": SLOW_REFRESH_S}
        )
    if fault is Fault.not_found:
        return templates.TemplateResponse(request, "not_found.html")
    return _result(request, member_id)


@app.get("/console/mbrlookup/result", response_model=None)
async def ticket_result(request: Request) -> Response:
    if not _signed_on(request):
        return _to_login()
    member_id = _tickets.pop(request.query_params.get("t", ""), None)
    if member_id is None:
        return RedirectResponse("/console/mbrlookup", status_code=303)
    return _result(request, member_id)
