"""Locator strategies and MatchRule on synthetic pages (page.set_content, no mockbank)."""

from __future__ import annotations

import pytest

from cua.schema.artifact import MatchRule, Target
from cua.surface.base import TargetAmbiguous, TargetNotFound
from tests.conftest import log_events

ACCOUNTS = """
<table border="1">
  <tr><th>{h1}</th><th>{h2}</th><th>{h3}</th></tr>
  <tr><td>Share Draft Checking</td><td>{c1}</td><td>Active</td></tr>
  <tr><td>Share Savings</td><td>{c2}</td><td>Dormant</td></tr>
</table>
"""


def target(*locators: dict, frame_path: list[str] | None = None, **match) -> Target:
    return Target.model_validate(
        {
            "description": "test target",
            "locators": list(locators),
            "frame_path": frame_path or [],
            "match": match,
            "rationale": "test",
        }
    )


def cell(row: str = "Share Savings", col: str = "Balance", header: str = "Account Type") -> dict:
    return {"strategy": "table_cell", "table_has_header": header, "row_contains": row, "column_header": col}


@pytest.fixture
def surface(make_surface):
    return make_surface(targets={})


async def text_of(surface, t: Target) -> str:
    return await surface.read(await surface.resolve("t", t))


# ---- table_cell ------------------------------------------------------------------------------- #


async def test_table_cell_by_row_and_column(session, surface):
    await session.page.set_content(
        ACCOUNTS.format(h1="Account Type", h2="Balance", h3="Status", c1="$1.00", c2="$2.00")
    )
    assert await text_of(surface, target(cell())) == "$2.00"
    assert await text_of(surface, target(cell(col="Status"))) == "Dormant"


async def test_table_cell_survives_column_reordering(session, surface):
    html = """<table><tr><th>Status</th><th>Account Type</th><th>Balance</th></tr>
      <tr><td>Active</td><td>Share Savings</td><td>$9.99</td></tr></table>"""
    await session.page.set_content(html)
    assert await text_of(surface, target(cell())) == "$9.99"


async def test_table_cell_header_in_td_and_nested_layout_table(session, surface):
    html = """<table><tr><td>Layout chrome</td><td>
        <table><tr><td>Account Type</td><td>Balance</td></tr>
               <tr><td>Share Savings</td><td>$5.00</td></tr></table>
      </td></tr></table>"""
    await session.page.set_content(html)
    assert await text_of(surface, target(cell())) == "$5.00"


async def test_table_cell_quotes_in_text(session, surface):
    html = """<table><tr><th>Member's "Type"</th><th>Balance</th></tr>
      <tr><td>O'Brien "joint"</td><td>$3.00</td></tr></table>"""
    await session.page.set_content(html)
    t = target(cell(row='O\'Brien "joint"', header='Member\'s "Type"'))
    assert await text_of(surface, t) == "$3.00"


async def test_table_cell_missing_row_is_not_found(session, surface):
    await session.page.set_content(
        ACCOUNTS.format(h1="Account Type", h2="Balance", h3="Status", c1="$1.00", c2="$2.00")
    )
    with pytest.raises(TargetNotFound):
        await surface.resolve("t", target(cell(row="Certificate")))


async def test_table_cell_duplicate_row_is_ambiguous(session, surface):
    await session.page.set_content(
        ACCOUNTS.format(h1="Account Type", h2="Balance", h3="Status", c1="$1.00", c2="$2.00")
    )
    with pytest.raises(TargetAmbiguous):
        await surface.resolve("t", target(cell(row="Share")))


# ---- near_text -------------------------------------------------------------------------------- #

FORM = """
<table>
  <tr><td>Member Number</td><td><input type="checkbox" name="cb"><input type="text" name="mbr"></td></tr>
  <tr><td>Branch</td><td><select name="br"><option>Main</option></select></td></tr>
</table>
<div style="position:absolute; left:400px; top:200px">Notes</div>
<textarea name="notes" style="position:absolute; left:400px; top:230px"></textarea>
"""


async def name_of(surface, t: Target) -> str:
    resolved = await surface.resolve("t", t)
    return str(await resolved.handle.get_attribute("name"))


def near(anchor: str, relation: str, role: str | None = None) -> dict:
    return {"strategy": "near_text", "anchor_text": anchor, "relation": relation, "role": role}


async def test_near_text_following_input_with_role_filter(session, surface):
    await session.page.set_content(FORM)
    assert await name_of(surface, target(near("Member Number", "following_input"))) == "cb"
    assert await name_of(surface, target(near("Member Number", "following_input", "textbox"))) == "mbr"


async def test_near_text_following_input_stays_in_row(session, surface):
    await session.page.set_content(FORM)
    with pytest.raises(TargetNotFound):  # the only textbox after "Branch" is outside its row
        await surface.resolve("t", target(near("Branch", "following_input", "textbox")))
    assert await name_of(surface, target(near("Branch", "following_input", "combobox"))) == "br"


async def test_near_text_right_of_and_below(session, surface):
    await session.page.set_content(FORM)
    assert await name_of(surface, target(near("Branch", "right_of"))) == "br"
    assert await name_of(surface, target(near("Notes", "below"))) == "notes"


async def test_near_text_missing_anchor(session, surface):
    await session.page.set_content(FORM)
    with pytest.raises(TargetNotFound):
        await surface.resolve("t", target(near("Account Holder #", "following_input")))


# ---- ranking, fallbacks, coordinates ---------------------------------------------------------- #


async def test_fallback_locator_wins_and_drift_is_logged(session, surface, run_logger):
    await session.page.set_content('<form name="f"><input type="submit" value="Go"></form>')
    t = target(
        {"strategy": "role", "role": "button", "name": "Search"},
        {"strategy": "xpath", "xpath": "//form[@name='f']/input"},
    )
    resolved = await surface.resolve("go_button", t)
    assert resolved.locator_index == 1
    [ev] = [e for e in log_events(run_logger) if e["event"] == "target_resolved"]
    assert ev["details"] == {"target": "go_button", "strategy": "xpath", "locator_index": 1}


async def test_css_locator(session, surface):
    await session.page.set_content('<div class="a">x</div><div class="b">y</div>')
    t = target({"strategy": "text", "text": "nope"}, {"strategy": "css", "selector": "div.b"})
    assert await text_of(surface, t) == "y"


COORD_PAGE = """
<body style="margin:0">
  <iframe name="inner"
    style="position:absolute; left:100px; top:100px; width:400px; height:200px; border:2px solid"
    srcdoc="<body style='margin:0'><button style='width:400px;height:200px'>Inside</button></body>"></iframe>
</body>
"""


async def test_coordinates_descend_into_iframe(session, surface):
    await session.page.set_content(COORD_PAGE)
    await session.page.frame(name="inner").wait_for_load_state()
    # (300, 200) px in a 1280x800 viewport lands inside the iframe's button
    t = target(
        {"strategy": "text", "text": "absent"}, {"strategy": "coordinates", "x": 300 / 1280, "y": 0.25}
    )
    resolved = await surface.resolve("t", t)
    assert resolved.locator_index == 1
    # read() would refuse here: the gate does not allowlist about:srcdoc frames (conservative, Phase 0)
    assert await resolved.handle.inner_text() == "Inside"


async def test_coordinates_rejected_by_text_pattern(session, surface):
    await session.page.set_content(COORD_PAGE)
    await session.page.frame(name="inner").wait_for_load_state()
    t = target(
        {"strategy": "text", "text": "absent"},
        {"strategy": "coordinates", "x": 300 / 1280, "y": 0.25},
        text_pattern="^OK$",
    )
    with pytest.raises(TargetNotFound):
        await surface.resolve("t", t)


# ---- MatchRule -------------------------------------------------------------------------------- #


async def test_match_rule_visible(session, surface):
    await session.page.set_content('<div style="display:none">Secret Panel</div>')
    t = target({"strategy": "text", "text": "Secret Panel"})
    with pytest.raises(TargetNotFound):
        await surface.resolve("t", t)
    t_hidden_ok = target({"strategy": "text", "text": "Secret Panel"}, visible=False)
    assert (await surface.resolve("t", t_hidden_ok)).locator_index == 0


async def test_match_rule_enabled(session, surface):
    await session.page.set_content('<input type="submit" value="Search" disabled>')
    with pytest.raises(TargetNotFound):
        await surface.resolve(
            "t", target({"strategy": "role", "role": "button", "name": "Search"}, enabled=True)
        )


async def test_match_rule_text_pattern_error_never_echoes_text(session, surface):
    await session.page.set_content(
        ACCOUNTS.format(h1="Account Type", h2="Balance", h3="Status", c1="$1.00", c2="Pending 4417")
    )
    t = target(cell(), text_pattern=MatchRule().text_pattern or r"^\$[0-9,]+\.[0-9]{2}$")
    with pytest.raises(TargetNotFound) as exc:
        await surface.resolve("bal", t)
    assert "4417" not in str(exc.value) and "Pending" not in str(exc.value)


async def test_unique_false_takes_first(session, surface):
    await session.page.set_content("<p>Row</p><p>Row</p>")
    with pytest.raises(TargetAmbiguous):
        await surface.resolve("t", target({"strategy": "text", "text": "Row", "exact": True}))
    t = target({"strategy": "text", "text": "Row", "exact": True}, unique=False)
    assert (await surface.resolve("t", t)).locator_index == 0
