"""Guardrails: one gate used by Surface for both the LLM and replay."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from cua.config import Policy
from cua.schema.artifact import RiskClass

Mode = Literal["discovery", "replay"]


class PolicyViolation(Exception):
    pass


class NeedsHuman(Exception):
    """Raised when policy says a human must decide (e.g. irreversible action during discovery)."""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._origins = {o.rstrip("/") for o in policy.allowed_origins}

    def origin_allowed(self, url: str) -> bool:
        if url.startswith(("data:", "about:blank")):
            return True
        return _origin(url) in self._origins

    def authorize(
        self,
        *,
        action_type: str,
        risk: RiskClass,
        mode: Mode,
        url: str | None = None,
        confirmed: bool = False,
    ) -> None:
        """Raise PolicyViolation / NeedsHuman, or return None if the action may proceed."""
        if action_type not in self.policy.allowed_actions:
            raise PolicyViolation(f"action type '{action_type}' is not allowed")
        if url is not None and not self.origin_allowed(url):
            raise PolicyViolation(f"origin {_origin(url)} is not on the allowlist")
        if risk is RiskClass.irreversible:
            if mode == "discovery":
                if self.policy.risk.discovery_irreversible == "block":
                    raise PolicyViolation("irreversible actions are blocked during discovery")
                raise NeedsHuman("irreversible action during discovery requires a human")
            if self.policy.risk.replay_irreversible == "block" or not confirmed:
                raise PolicyViolation("irreversible action requires caller confirmation")

    def is_irreversible_name(self, accessible_name: str | None) -> bool:
        name = (accessible_name or "").strip().lower()
        return any(word in name for word in self.policy.risk.irreversible_button_names)
