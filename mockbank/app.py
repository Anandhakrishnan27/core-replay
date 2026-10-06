"""MockBank Core (target app). Phase 1.

Legacy ON PURPOSE. Do not "clean up":
  - frameset: left `nav` frame + `main` frame
  - table-based layouts, no ids / data-testid, no <label for>
  - server-rendered HTML only, no API

Routes (all HTML):
  GET  /login, POST /login           session cookie (credentials from env)
  GET  /console                      frameset (nav + main)
  GET  /console/nav                  navigation links
  GET  /console/home                 landing page ("MockBank Core v2.3")
  GET  /console/mbrlookup            lookup form: "Member Number" + Search
  POST /console/mbrlookup            → member summary | "No member found" | fault pages
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="MockBank Core", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/console", response_class=HTMLResponse)
async def console(request: Request) -> HTMLResponse:
    # TODO(phase-1): require session; set fault cookie if ?fault= present; render frameset
    return templates.TemplateResponse(request, "frameset.html")


# TODO(phase-1): /login (GET/POST), /console/nav, /console/home,
#                /console/mbrlookup (GET form, POST search) with fault handling from faults.py
