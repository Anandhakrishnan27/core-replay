"""Load guardrail policy and tenant configuration from config/."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

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


class Tenant(_Cfg):
    tenant_id: str
    product: str
    product_version: str
    base_url: str
    credentials: Credentials
    overrides: dict[str, dict[str, Any]] = {}


def load_policy(path: Path | None = None) -> Policy:
    return Policy.model_validate(yaml.safe_load((path or CONFIG_DIR / "policy.yaml").read_text()))


def load_tenant(tenant_id: str, config_dir: Path | None = None) -> Tenant:
    path = (config_dir or CONFIG_DIR) / "tenants" / f"{tenant_id}.yaml"
    return Tenant.model_validate(yaml.safe_load(path.read_text()))
