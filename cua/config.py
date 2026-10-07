"""Load guardrail policy and tenant configuration from config/."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
CAPABILITIES_DIR = ROOT / "capabilities"
EVIDENCE_DIR = ROOT / "evidence"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskPolicy(_Cfg):
    irreversible_button_names: list[str]
    discovery_irreversible: Literal["escalate", "block"]
    replay_irreversible: Literal["require_confirmation", "block"]


class RedactionPolicy(_Cfg):
    hash_salt_env: str
    mask_sensitive_targets_in_screenshots: bool = True


class Limits(_Cfg):
    discovery_max_steps: int = 25
    discovery_timeout_s: int = 300
    repeated_observation_limit: int = 3
    step_timeout_ms: int = 10_000
    poll_interval_ms: int = 250
    handoff_timeout_s: int = 900


class Policy(_Cfg):
    version: int
    allowed_origins: list[str]
    allowed_actions: list[str]
    risk: RiskPolicy
    unattended_requires_review_status: Literal["approved", "draft"] = "approved"
    redaction: RedactionPolicy
    limits: Limits = Limits()


class Credentials(_Cfg):
    username_env: str
    password_env: str


class RevealRule(_Cfg):
    """Partial reveal of one masked value in EVIDENCE SCREENSHOTS only (bank-style "last 4").

    Applies to leaf cells matched by `selector`, which must also be one of the tenant's mask_selectors,
    and, with `caption`, only when the previous sibling cell's text equals it (legacy label/value rows).
    Styles: `last` keeps the last `keep` letters/digits (separators kept, the rest •), `initials` keeps
    each word's first letter, `email` keeps the first character and the domain, `keep` shows the value.
    DOM dumps, logs and the discovery LLM always see the value fully masked.
    """

    selector: str
    caption: str | None = None
    style: Literal["last", "initials", "email", "keep"]
    keep: int = Field(default=4, ge=1, le=4)


class Tenant(_Cfg):
    tenant_id: str
    product: str
    product_version: str
    base_url: str
    credentials: Credentials
    overrides: dict[str, dict[str, Any]] = {}
    # CSS selectors (applied in every frame) for PII that is on screen but is not an artifact target.
    # Masked in screenshots; text replaced with «masked» in DOM dumps.
    mask_selectors: list[str] = []
    # Partial reveals for evidence screenshots. Empty → every masked value is hidden completely.
    screenshot_reveal: list[RevealRule] = []

    @model_validator(mode="after")
    def _reveal_only_masked(self) -> Tenant:
        for rule in self.screenshot_reveal:
            if rule.selector not in self.mask_selectors:
                raise ValueError(f"screenshot_reveal selector {rule.selector!r} is not in mask_selectors")
        return self


def load_policy(path: Path | None = None) -> Policy:
    return Policy.model_validate(yaml.safe_load((path or CONFIG_DIR / "policy.yaml").read_text()))


def load_tenant(tenant_id: str, config_dir: Path | None = None) -> Tenant:
    path = (config_dir or CONFIG_DIR) / "tenants" / f"{tenant_id}.yaml"
    return Tenant.model_validate(yaml.safe_load(path.read_text()))
