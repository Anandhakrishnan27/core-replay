"""SessionProvider: login / reauthenticate in the same context, credential hygiene, tracing."""

from __future__ import annotations

import zipfile
from urllib.parse import urlsplit

import pytest

from cua.config import Credentials
from cua.evidence.logger import RunLogger
from cua.schema.artifact import Check
from cua.session.provider import LoginFailed, MissingCredentials, SessionProvider
from cua.surface.browser import open_session
from cua.surface.wait import poll_until
from tests.conftest import TEST_PASSWORD


def with_env_names(tenant, user_env: str, password_env: str):
    return tenant.model_copy(
        update={"credentials": Credentials(username_env=user_env, password_env=password_env)}
    )


async def test_login_reaches_console(session, tenant):
    await SessionProvider(tenant).login(session)
    assert urlsplit(session.page.url).path == "/console"
    assert session.page.frame(name="nav") is not None


async def test_wrong_password_fails_without_echoing_it(session, tenant, monkeypatch):
    monkeypatch.setenv("CUA_TEST_USER", "teller01")
    monkeypatch.setenv("CUA_TEST_BAD_PW", "nope-not-it")
    provider = SessionProvider(with_env_names(tenant, "CUA_TEST_USER", "CUA_TEST_BAD_PW"))
    with pytest.raises(LoginFailed) as exc:
        await provider.login(session)
    assert "nope-not-it" not in str(exc.value)


async def test_missing_credentials_before_touching_browser(session, tenant, monkeypatch):
    monkeypatch.delenv("CUA_TEST_NOPE_U", raising=False)
    monkeypatch.delenv("CUA_TEST_NOPE_P", raising=False)
    provider = SessionProvider(with_env_names(tenant, "CUA_TEST_NOPE_U", "CUA_TEST_NOPE_P"))
    with pytest.raises(MissingCredentials):
        await provider.login(session)
    assert session.page.url == "about:blank"
    assert await provider.reauthenticate(session) is False


async def test_reauthenticate_in_same_context(session, tenant, make_surface, artifact, mockbank_url):
    provider = SessionProvider(tenant)
    await provider.login(session)
    context, page = session.context, session.page
    await context.add_cookies([{"name": "mb_fault", "value": "session_expired", "url": mockbank_url}])
    surface = make_surface()
    targets = dict(artifact.targets)
    expired = artifact.conditions["session_expired"].detect
    lookup_ready = next(s for s in artifact.steps if s.id == "go_to_lookup").expect
    assert lookup_ready is not None

    async def holds(check: Check) -> bool:
        return any([await surface.check(p, targets) for p in check.any_of]) or (
            bool(check.all_of) and all([await surface.check(p, targets) for p in check.all_of])
        )

    nav = await surface.resolve("nav_member_lookup", artifact.targets["nav_member_lookup"])
    await nav.handle.click()
    assert await poll_until(lambda: holds(expired), 5000, 100)

    assert await provider.reauthenticate(session) is True
    assert session.context is context and session.page is page and context.pages == [page]

    # Retry the step: the once-fault is spent (cookie survived re-login), so the form opens.
    nav = await surface.resolve("nav_member_lookup", artifact.targets["nav_member_lookup"])
    await nav.handle.click()
    assert await poll_until(lambda: holds(lookup_ready), 5000, 100)


async def test_tracing_off_by_default(session, tmp_path):
    assert session.trace_dir is None and session.trace_files == []


async def test_trace_goes_to_scratch_and_never_contains_the_password(
    browser, test_policy, gate, tenant, tmp_path, mockbank_url
):
    scratch = tmp_path / "scratch"
    logger = RunLogger(tmp_path / "ev", "traced", "replay")
    async with open_session(browser, test_policy, gate, logger, trace=True, scratch_root=scratch) as s:
        await SessionProvider(tenant).login(s)
        await s.page.goto(f"{mockbank_url}/console/mbrlookup")
        assert await SessionProvider(tenant).reauthenticate(s)
        await s.page.goto(f"{mockbank_url}/console/home")
    files = sorted(scratch.rglob("*.zip"))
    assert files and all(f.is_relative_to(scratch / "traces") for f in files)
    recorded = 0
    for f in files:
        with zipfile.ZipFile(f) as z:
            for name in z.namelist():
                blob = z.read(name)
                assert TEST_PASSWORD.encode() not in blob, f"{f.name}:{name}"
                recorded += b"/console/mbrlookup" in blob
    assert recorded  # the automated part IS traced
