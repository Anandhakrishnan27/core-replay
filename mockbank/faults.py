"""Fault injection: makes runtime errors reproducible for replay tests and evidence runs.

Activate with the `?fault=<name>` query param (sticky for the session via cookie) or the
MOCKBANK_FAULT env var. `?fault=none` clears the cookie.

Every fault is deterministic. Faults marked ONCE fire on their first trigger per browser context,
tracked in the `mb_once` cookie, which survives re-login in the same context.
"""

from __future__ import annotations

import os
from enum import Enum

from fastapi import Request
from starlette.responses import Response


class Fault(str, Enum):
    none = "none"
    not_found = "not_found"  # every search returns "No member found"
    notice = "notice"  # ONCE: "System Notice" interstitial on opening the lookup form
    slow = "slow"  # "Processing, please wait" page that refreshes to the result after 2s
    session_expired = "session_expired"  # ONCE: opening the lookup form bounces to /login?expired=1
    denied = "denied"  # every search: "You are not authorized"
    error = "error"  # every search: "An unexpected error has occurred"
    maint = "maint"  # ONCE: unknown "Maintenance Window" page on search (UNKNOWN_STATE)


FAULT_COOKIE = "mb_fault"
ONCE_COOKIE = "mb_once"


def active_fault(request: Request) -> Fault:
    raw = (
        request.query_params.get("fault") or request.cookies.get(FAULT_COOKIE) or os.getenv("MOCKBANK_FAULT")
    )
    try:
        return Fault(raw) if raw else Fault.none
    except ValueError:
        return Fault.none


def apply_fault_param(request: Request, response: Response) -> None:
    """Make `?fault=<name>` sticky. `none` clears the fault and the once-flags."""
    raw = request.query_params.get("fault")
    if raw is None:
        return
    try:
        fault = Fault(raw)
    except ValueError:
        return
    if fault is Fault.none:
        response.delete_cookie(FAULT_COOKIE)
        response.delete_cookie(ONCE_COOKIE)
    else:
        response.set_cookie(FAULT_COOKIE, fault.value, httponly=True, samesite="lax")


def once_consumed(request: Request, fault: Fault) -> bool:
    return fault.value in request.cookies.get(ONCE_COOKIE, "").split(",")


def mark_once(request: Request, response: Response, fault: Fault) -> None:
    used = {v for v in request.cookies.get(ONCE_COOKIE, "").split(",") if v}
    used.add(fault.value)
    response.set_cookie(ONCE_COOKIE, ",".join(sorted(used)), httponly=True, samesite="lax")
