"""MockBank (target app) at the HTML level: routes, the UI contract the fixture artifact relies on, faults.

Browser-level checks (frames, locators) come with the replay tests in Phase 3.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mockbank.app import app

TEMPLATES = Path(__file__).resolve().parents[1] / "mockbank/templates"


@pytest.fixture(autouse=True)
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCKBANK_USER", "teller01")
    monkeypatch.setenv("MOCKBANK_PASSWORD", "pw-test")
    monkeypatch.delenv("MOCKBANK_FAULT", raising=False)


def _sign_on(client: TestClient) -> None:
    r = client.post("/login", data={"userid": "teller01", "password": "pw-test"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/console"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        _sign_on(c)
        yield c


def _with_fault(client: TestClient, fault: str) -> None:
    assert client.get(f"/console?fault={fault}").status_code == 200


def _search(client: TestClient, member_id: str):
    return client.post("/console/mbrlookup", data={"mbrno": member_id}, follow_redirects=False)


# --- markup contract -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_templates_stay_legacy(path: Path) -> None:
    html = path.read_text()
    assert not re.search(r"\sid\s*=", html)
    assert "data-testid" not in html
    assert "<label" not in html
    assert "aria-" not in html


# --- sign-on -------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/console", "/console/nav", "/console/home", "/console/mbrlookup"])
def test_console_requires_session(path: str) -> None:
    with TestClient(app) as c:
        r = c.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_page_hooks() -> None:
    with TestClient(app) as c:
        html = c.get("/login").text
    assert '<form name="signon" method="post" action="/login" target="_top">' in html
    assert 'name="userid"' in html and 'type="password" name="password"' in html
    assert 'value="Sign On"' in html
    assert "Your session has expired" not in html


def test_bad_password_rejected() -> None:
    with TestClient(app) as c:
        r = c.post("/login", data={"userid": "teller01", "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 401
        assert "Invalid user ID or password" in r.text
        assert c.get("/console", follow_redirects=False).status_code == 303


def test_login_refused_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOCKBANK_USER")
    monkeypatch.delenv("MOCKBANK_PASSWORD")
    with TestClient(app) as c:
        r = c.post("/login", data={"userid": "", "password": ""}, follow_redirects=False)
    assert r.status_code == 401


# --- normal flow ---------------------------------------------------------------------------------


def test_frameset_nav_home(client: TestClient) -> None:
    frameset = client.get("/console").text
    assert '<frame name="nav" src="/console/nav">' in frameset
    assert '<frame name="main" src="/console/home">' in frameset
    nav = client.get("/console/nav").text
    assert '<a href="/console/mbrlookup" target="main">Member Lookup</a>' in nav
    assert "MockBank Core v2.3" in client.get("/console/home").text


def test_lookup_form_hooks(client: TestClient) -> None:
    html = client.get("/console/mbrlookup").text
    assert '<form name="lookup" method="post" action="/console/mbrlookup">' in html
    # caption in the cell right before the input: near_text / following_input
    assert re.search(r'<td class="cap">Member Number</td><td><input type="text" name="mbrno"', html)
    assert html.count('type="submit"') == 1 and 'value="Search"' in html


@pytest.mark.parametrize(
    "member_id,first_row",
    [("10001", "Share Savings"), ("10002", "Share Draft Checking")],
)
def test_member_summary(client: TestClient, member_id: str, first_row: str) -> None:
    html = _search(client, member_id).text
    assert '<td class="hdr">Member Summary</td>' in html
    assert "<tr><th>Account Type</th><th>Balance</th><th>Status</th></tr>" in html
    rows = re.findall(r"<tr><td>([^<]+)</td><td class=\"amt\">(\$[0-9,]+\.[0-9]{2})</td>", html)
    assert rows[0][0] == first_row
    assert "Share Savings" in [r[0] for r in rows]


def test_member_without_savings(client: TestClient) -> None:
    html = _search(client, "10003").text
    assert "Member Summary" in html
    assert "Share Savings" not in html


def test_unknown_member(client: TestClient) -> None:
    r = _search(client, "99999")
    assert "No member found" in r.text
    assert "99999" not in r.text  # the number entered is not echoed


# --- fault cookie --------------------------------------------------------------------------------


def test_fault_param_is_sticky_and_clearable(client: TestClient) -> None:
    _with_fault(client, "not_found")
    assert client.cookies.get("mb_fault") == "not_found"
    assert "No member found" in _search(client, "10001").text
    _with_fault(client, "none")
    assert client.cookies.get("mb_fault") is None
    assert "Member Summary" in _search(client, "10001").text


def test_unknown_fault_name_ignored(client: TestClient) -> None:
    _with_fault(client, "bogus")
    assert client.cookies.get("mb_fault") is None


def test_fault_from_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCKBANK_FAULT", "error")
    assert "An unexpected error has occurred" in _search(client, "10001").text


# --- faults --------------------------------------------------------------------------------------


def test_fault_not_found(client: TestClient) -> None:
    _with_fault(client, "not_found")
    assert "No member found" in _search(client, "10001").text


def test_fault_denied(client: TestClient) -> None:
    _with_fault(client, "denied")
    r = _search(client, "10001")
    assert r.status_code == 403 and "You are not authorized" in r.text
    assert "Member Summary" not in r.text


def test_fault_error(client: TestClient) -> None:
    _with_fault(client, "error")
    r = _search(client, "10001")
    assert r.status_code == 500 and "An unexpected error has occurred" in r.text


def test_fault_notice_shows_once(client: TestClient) -> None:
    _with_fault(client, "notice")
    first = client.get("/console/mbrlookup").text
    assert '<div class="sysnotice">' in first and "System Notice" in first
    assert '<input type="submit" value="OK">' in first
    assert 'name="mbrno"' not in first
    # OK reloads the form; the notice does not come back
    second = client.get("/console/mbrlookup").text
    assert "System Notice" not in second and 'name="mbrno"' in second


def test_fault_slow_refreshes_to_result(client: TestClient) -> None:
    _with_fault(client, "slow")
    html = _search(client, "10001").text
    assert "Processing, please wait" in html
    assert "10001" not in html  # ticket, not the member number, in the URL
    url = re.search(r'content="2;url=([^"]+)"', html)
    assert url is not None
    assert "Member Summary" in client.get(url.group(1)).text
    # tickets are single-use
    r = client.get(url.group(1), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/console/mbrlookup"


def test_fault_session_expired_once(client: TestClient) -> None:
    _with_fault(client, "session_expired")
    r = client.get("/console/mbrlookup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?expired=1"
    assert "Your session has expired" in client.get("/login?expired=1").text
    # session really is gone
    assert client.get("/console/nav", follow_redirects=False).status_code == 303
    # re-authenticate in the same context: the fault does not fire again
    _sign_on(client)
    assert 'name="mbrno"' in client.get("/console/mbrlookup").text
    assert "Member Summary" in _search(client, "10001").text


def test_fault_maint_once_with_retry(client: TestClient) -> None:
    _with_fault(client, "maint")
    r = _search(client, "10001")
    assert r.status_code == 503 and "Maintenance Window" in r.text
    # must not match any known condition text in the fixture artifact
    for known in (
        "No member found",
        "Processing, please wait",
        "Your session has expired",
        "You are not authorized",
        "An unexpected error has occurred",
        "System Notice",
        "Member Summary",
    ):
        assert known not in r.text
    retry = re.search(r'<a href="([^"]+)">Try Again</a>', r.text)
    assert retry is not None
    assert "Member Summary" in client.get(retry.group(1)).text
    # second search goes through normally
    assert "Member Summary" in _search(client, "10001").text
