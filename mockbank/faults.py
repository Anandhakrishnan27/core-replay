"""Fault injection: makes runtime errors reproducible for replay tests and evidence runs.

Activate with the `?fault=<name>` query param (sticky for the session via cookie) or the
MOCKBANK_FAULT env var.
"""

from __future__ import annotations

import os
from enum import Enum

from fastapi import Request


class Fault(str, Enum):
    none = "none"
    not_found = "not_found"  # search always returns "No member found"
    notice = "notice"  # random "System Notice" interstitial after navigation
    slow = "slow"  # "Processing, please wait" page before results
    session_expired = "session_expired"  # bounces to /login once mid-flow
    denied = "denied"  # "You are not authorized" on member detail
    error = "error"  # "An unexpected error has occurred"
    maint = "maint"  # unknown "Maintenance Window" page (UNKNOWN_STATE)


FAULT_COOKIE = "mb_fault"


def active_fault(request: Request) -> Fault:
    raw = (
        request.query_params.get("fault") or request.cookies.get(FAULT_COOKIE) or os.getenv("MOCKBANK_FAULT")
    )
    try:
        return Fault(raw) if raw else Fault.none
    except ValueError:
        return Fault.none
