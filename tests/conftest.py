from __future__ import annotations

import copy
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from playwright.async_api import Browser

from cua import catalog
from cua.config import Policy, Tenant, load_policy, load_tenant
from cua.evidence.logger import RunLogger
from cua.safety.policy import PolicyGate
from cua.schema.artifact import CapabilityArtifact
from cua.surface.browser import BrowserSession, open_session, start_browser
from cua.surface.playwright_web import PlaywrightWebSurface

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "tests/fixtures/lookup_savings_balance.handwritten.json"


@pytest.fixture
def policy():
    return load_policy(ROOT / "config/policy.yaml")


@pytest.fixture
def raw_artifact() -> dict:
    """The fixture artifact, ALWAYS as a draft: tests never depend on whether someone ran `cua approve`."""
    raw = json.loads(ARTIFACT_PATH.read_text())
    raw["review"] = {"status": "draft"}
    return raw


@pytest.fixture
def artifact(raw_artifact) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(raw_artifact)


@pytest.fixture
def catalog_root(tmp_path_factory, raw_artifact) -> Path:
    """A temp catalog holding a draft copy of the fixture (never the real capabilities/ folder)."""
    root = tmp_path_factory.mktemp("catalog")
    catalog.save(CapabilityArtifact.model_validate(copy.deepcopy(raw_artifact)), root)
    return root


@pytest.fixture
def approved_artifact(catalog_root, artifact) -> CapabilityArtifact:
    """Approved through the real approval path, in the temp catalog."""
    catalog.approve(artifact.id, "tester", artifact.version, root=catalog_root)
    return catalog.load(artifact.id, artifact.version, root=catalog_root)


# --------------------------------------------------------------------------- #
# Browser tests: mockbank on a free port, one Chromium, a fresh context per test
# --------------------------------------------------------------------------- #

TEST_USER, TEST_PASSWORD = "teller01", "pw-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def mockbank_url() -> Iterator[str]:
    from mockbank.app import app

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MOCKBANK_USER", TEST_USER)
        mp.setenv("MOCKBANK_PASSWORD", TEST_PASSWORD)
        mp.delenv("MOCKBANK_FAULT", raising=False)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:  # bounded startup wait (test harness only)
            assert time.monotonic() < deadline, "mockbank did not start"
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def test_policy(policy, mockbank_url: str) -> Policy:
    return policy.model_copy(update={"allowed_origins": [mockbank_url]})


@pytest.fixture
def tenant(mockbank_url: str) -> Tenant:
    return load_tenant("cu_alpha", ROOT / "config").model_copy(update={"base_url": mockbank_url})


@pytest.fixture
def gate(test_policy: Policy) -> PolicyGate:
    return PolicyGate(test_policy)


@pytest.fixture
def run_logger(tmp_path: Path) -> RunLogger:
    return RunLogger(tmp_path, "test", "replay")


@pytest_asyncio.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with start_browser() as b:
        yield b


@pytest_asyncio.fixture
async def session(browser, test_policy, gate, run_logger) -> AsyncIterator[BrowserSession]:
    async with open_session(browser, test_policy, gate, run_logger) as s:
        yield s


@pytest.fixture
def make_surface(session, test_policy, gate, run_logger, artifact, tenant):
    def make(**kw) -> PlaywrightWebSurface:
        kw.setdefault("mode", "replay")
        kw.setdefault("targets", dict(artifact.targets))
        kw.setdefault("mask_selectors", tenant.mask_selectors)
        kw.setdefault("reveal_rules", tenant.screenshot_reveal)
        return PlaywrightWebSurface(session.page, test_policy, gate, run_logger, **kw)

    return make


def log_events(logger: RunLogger) -> list[dict]:
    path = logger.dir / "run.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
