"""Compiler: trace → artifact, offline (no browser, no LLM).

Input: tests/fixtures/discovery_trace.notice.json, recorded once by running the discovery agent with the
scripted stub against mockbank under the `notice` fault (synthetic member 10001). It is the RAW in-memory
trace, as the compiler receives it straight after a run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.compiler import CompileError
from cua.compiler.clean import clean
from cua.compiler.compile import compile_trace
from cua.compiler.conditions_library import load_pack
from cua.config import CONFIG_DIR, load_tenant
from cua.discovery.recorder import TraceRecorder
from cua.schema.artifact import (
    CapabilityArtifact,
    CssLocator,
    Fill,
    Navigate,
    RiskClass,
    TextPresent,
)
from cua.schema.trace import DiscoveryTrace, PageState, TraceAction

FIXTURE = Path(__file__).parent / "fixtures" / "discovery_trace.notice.json"
CAP_ID = "mockbank.member.lookup_savings_balance"


@pytest.fixture
def trace() -> DiscoveryTrace:
    return DiscoveryTrace.model_validate_json(FIXTURE.read_text())


@pytest.fixture
def compile_(policy):
    tenant = load_tenant("cu_alpha")

    def go(t: DiscoveryTrace, **kw) -> CapabilityArtifact:
        return compile_trace(t, CAP_ID, tenant=tenant, policy=kw.pop("policy", policy), **kw)

    return go


def with_action(t: DiscoveryTrace, seq: int, **update) -> DiscoveryTrace:
    actions = [a.model_copy(update=update) if a.seq == seq else a for a in t.actions]
    return t.model_copy(update={"actions": actions})


# ---- the whole pipeline ------------------------------------------------------------------------- #


def test_fixture_trace_compiles_to_the_target_shape(trace, compile_):
    art = compile_(trace)
    assert art.review.status.value == "draft" and art.version == "1.0.0"
    assert [s.action.type for s in art.steps] == ["navigate", "click", "fill", "click", "extract", "extract"]
    nav, lookup, fill, search, *_ = art.steps
    assert isinstance(nav.action, Navigate) and nav.action.url == "{{tenant.base_url}}/console"
    assert isinstance(fill.action, Fill) and fill.action.value == "{{inputs.member_id}}"

    (member_id,) = art.contract.inputs
    assert (member_id.name, member_id.type.value, member_id.sensitivity.value) == (
        "member_id",
        "string",
        "pii",
    )
    assert member_id.pattern == "^[0-9]+$" and member_id.example is None
    outputs = {o.name: (o.type.value, o.sensitivity.value) for o in art.contract.outputs}
    assert outputs == {"savings_balance": ("decimal", "pii"), "account_status": ("string", "internal")}
    assert [o.code for o in art.contract.outcomes] == ["SUCCESS", "MEMBER_NOT_FOUND", "NO_SHARE_SAVINGS"]

    # Locators: semantic first, verified, positional fallbacks gone.
    field_ = art.targets["member_number_field"]
    assert [loc.strategy for loc in field_.locators] == ["near_text", "css"] and field_.sensitive
    balance = art.targets["share_savings_balance_cell"]
    assert [loc.strategy for loc in balance.locators] == ["table_cell"]
    assert balance.match.text_pattern and balance.sensitive
    assert "positional" in balance.rationale

    # Checkpoints: next target + the heading that appeared; never the nav link text.
    assert lookup.expect is not None and [p.kind for p in lookup.expect.all_of] == ["target_visible"]
    assert search.expect is not None
    assert {getattr(p, "text", None) for p in search.expect.all_of} >= {"Member Summary"}

    # Conditions: business → recoverable → hard; the dismissed notice is covered by the product pack.
    kinds = [c.classification.value for c in art.conditions.values()]
    assert kinds == sorted(kinds, key=["business_outcome", "recoverable", "hard_failure"].index)
    assert art.conditions["member_not_found"].applies_to == ["click_search_button"]
    assert art.conditions["no_share_savings"].applies_to == ["click_search_button"]
    assert "system_notice" in art.conditions and "maint" not in json.dumps(art.model_dump(mode="json"))

    assert art.policy.max_risk is RiskClass.reversible and not art.policy.requires_confirmation
    assert art.app.version_range == ">=2.0,<3.0" and art.app.surface == "legacy_web"
    assert art.title == "Look up member <member_id> and read the savings balance"


def test_artifact_holds_no_values_and_no_tenant_host(trace, compile_):
    text = compile_(trace).model_dump_json()
    for raw in ["10001", "2,450.17", "2450.17", "Avery Quill", "127.0.0.1", "localhost"]:
        assert raw not in text, raw


def test_no_checkpoint_or_condition_asserts_data(trace, compile_):
    art = compile_(trace)
    checks = [art.success, art.app.fingerprint, *(s.expect for s in art.steps if s.expect)]
    checks += [c.detect for c in art.conditions.values()]
    for check in checks:
        for p in check.predicates():
            if isinstance(p, TextPresent):
                assert "«" not in p.text and not any(ch.isdigit() for ch in p.text), p.text
    for t in art.targets.values():
        for loc in t.locators:
            assert not (isinstance(loc, CssLocator) and "nth-of-type" in loc.selector)


def test_compiling_is_deterministic(trace, compile_):
    assert compile_(trace) == compile_(trace)


def test_saved_redacted_trace_compiles_to_the_same_artifact(trace, compile_):
    redacted = TraceRecorder(trace).redacted()
    assert "10001" not in redacted.model_dump_json()
    from_disk, from_memory = compile_(redacted), compile_(trace)
    assert from_disk.contract.inputs[0].pattern is None  # the digits-only hint needs the raw value
    strip = {"contract": {"inputs": {0: {"pattern"}}}}
    assert from_disk.model_dump(exclude=strip) == from_memory.model_dump(exclude=strip)


# ---- refusals ------------------------------------------------------------------------------------ #


def test_incomplete_discovery_is_not_compiled(trace, compile_):
    with pytest.raises(CompileError, match="escalated"):
        compile_(trace.model_copy(update={"status": "escalated"}))


def test_typed_value_that_is_not_a_parameter_is_refused(trace, compile_):
    with pytest.raises(CompileError, match="not one of the declared parameters") as e:
        compile_(with_action(trace, 3, value="99999"))
    assert "99999" not in str(e.value)


def test_target_without_a_verified_semantic_locator_is_refused(trace, compile_):
    fill = next(a for a in trace.actions if a.tool == "fill")
    assert fill.element is not None
    css_only = fill.element.model_copy(
        update={"verified_locators": [loc for loc in fill.element.verified_locators if loc.strategy == "css"]}
    )
    with pytest.raises(CompileError, match="no semantic locator"):
        compile_(with_action(trace, fill.seq, element=css_only))


# ---- passes -------------------------------------------------------------------------------------- #


def test_irreversible_button_makes_the_capability_require_confirmation(trace, compile_):
    search = next(a for a in trace.actions if a.tool == "click" and a.seq == 4)
    assert search.element is not None
    submit = search.element.model_copy(update={"accessible_name": "Submit Transfer"})
    art = compile_(with_action(trace, 4, element=submit))
    assert art.policy.max_risk is RiskClass.irreversible and art.policy.requires_confirmation


def page(url: str, *headings: str) -> PageState:
    return PageState(url=url, title="t", headings=list(headings))


def act(seq: int, tool: str, before: PageState, after: PageState, **kw) -> TraceAction:
    return TraceAction(
        seq=seq, at="2026-10-06T12:00:00Z", actor="llm", tool=tool, page_before=before, page_after=after, **kw
    )


def test_clean_drops_failures_keeps_last_fill_and_cuts_loops(trace):
    home, lookup, other = page("/c", "Home"), page("/c", "Lookup"), page("/c", "Other")
    actions = [
        act(1, "click", home, lookup),
        act(2, "click", lookup, other, ok=False, error="failed"),
        act(3, "click", lookup, other),  # wander off …
        act(4, "click", other, lookup),  # … and come back: a loop
        act(5, "fill", lookup, lookup, value="1"),
        act(6, "fill", lookup, lookup, value="2"),  # same element (no snapshot): last fill wins
    ]
    result = clean(trace.model_copy(update={"actions": actions}))
    assert [(a.seq, a.tool) for a in result.actions] == [(1, "click"), (6, "fill")]


def test_dismissed_dialog_moves_to_interstitials_and_fixes_the_previous_checkpoint(trace):
    result = clean(trace)
    assert [a.tool for a in result.interstitials] == ["dismiss"]
    lookup_click = result.actions[0]
    assert "System Notice" not in lookup_click.page_after.headings  # the real next screen


def test_without_a_pack_the_dismissed_dialog_becomes_an_observed_condition(trace, compile_, tmp_path):
    art = compile_(trace, pack_dir=tmp_path)  # empty dir: no product pack
    notice = art.conditions["system_notice"]
    assert notice.classification.value == "recoverable" and notice.handler.do == "dismiss"
    target = art.targets[notice.handler.target]  # type: ignore[union-attr]
    assert target.frame_path == [] and target.locators[0].strategy == "role"
    assert "session_expired" not in art.conditions  # pack-only knowledge
    assert "no_share_savings" in art.conditions  # derived, needs no pack


def test_product_pack_is_valid_data():
    pack = load_pack("mockbank_core", CONFIG_DIR / "products")
    assert set(pack.conditions) >= {"system_notice", "session_expired", "permission_denied", "app_error"}
    assert "maint" not in json.dumps(pack.conditions)


def test_pack_for_the_wrong_product_is_refused(tmp_path):
    (tmp_path / "mockbank_core.conditions.yaml").write_text("product: other_product\nconditions: {}\n")
    with pytest.raises(CompileError, match="other_product"):
        load_pack("mockbank_core", tmp_path)
