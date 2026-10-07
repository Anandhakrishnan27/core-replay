"""Discovery handoff: the model asks for help, a human acts in the SAME live session, discovery resumes.

The human's clicks / fills become TraceAction(actor="human") with verified locators, the compiler lists them
in provenance.human_assisted_steps, and a typed value never appears in run.jsonl, trace.json, the model's
messages, the operator API or the compiled artifact (only whether it matched a --param is kept).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest

from cua.compiler import CompileError
from cua.compiler.compile import compile_trace
from cua.discovery.agent import DiscoveryResult, discover
from cua.discovery.recorder import load_trace
from cua.handoff.operator import operator_server
from cua.replay.executor import execute
from cua.schema.artifact import Fill, RiskClass
from cua.schema.result import RunStatus
from tests.test_discovery_agent import GOAL, PARAMS, Response, StubMessages, call, cell_right_of
from tests.test_replay_handoff import Operator, _free_port

CAP_ID = "mockbank.member.lookup_savings_balance"


class RecordingStub(StubMessages):
    """Also keeps the text of the newest user turn of every request (what the model was just told)."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__(script)
        self.turns: list[str] = []

    async def create(self, **kwargs: Any) -> Response:
        content = kwargs["messages"][-1]["content"]
        blocks = (
            [content] if isinstance(content, str) else [b.get("text") or b.get("content") for b in content]
        )
        self.turns.append("\n".join(str(b) for b in blocks if b))
        return await super().create(**kwargs)


@pytest.fixture
def discover_with_operator(browser, tenant, test_policy, tmp_path):
    async def go(script: list[Any], human) -> tuple[DiscoveryResult, RecordingStub, dict[str, Any]]:
        stub = RecordingStub(script)
        seen: dict[str, Any] = {}
        async with operator_server(port=_free_port()) as op:
            async with httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{op.port}", headers={"X-Operator-Token": op.token}
            ) as http:
                person = Operator(op, http, browser)

                async def play() -> None:
                    await human(person)
                    # What the operator page shows, captured before hand-back ends the run's registration.
                    seen["api_actions"] = (await http.get(f"/api/runs/{person.run['run_id']}/actions")).text

                acting = asyncio.create_task(play())
                result = await discover(
                    GOAL,
                    tenant,
                    test_policy,
                    PARAMS,
                    messages_api=stub,
                    browser=browser,
                    evidence_root=tmp_path,
                    operator=op,
                    handoff_timeout_s=20,
                )
                await asyncio.wait_for(acting, 5)
                assert op.runs == {}, "discovery unregisters when it ends"
        return result, stub, seen

    return go


async def open_lookup_and_type(person: Operator, value: str) -> None:
    await person.paused()
    await person.take()
    nav = person.main().page.frame(name="nav")
    assert nav is not None
    await nav.get_by_text("Member Lookup").click()
    field = person.main().locator("input[name=mbrno]")
    await field.wait_for()
    await field.fill(value)


def evidence_text(result: DiscoveryResult) -> str:
    return (result.evidence_dir / "run.jsonl").read_text() + result.trace_path.read_text()


# ---- the human does the lookup; the model reads the balance ------------------------------------- #


async def test_human_steps_become_trace_steps_and_compile(
    discover_with_operator, tenant, test_policy, browser, tmp_path
):
    async def human(person: Operator) -> None:
        await open_lookup_and_type(person, "10001")
        await person.main().locator("input[type=submit]").click()  # blurs the field: `change` fires first
        await person.main().get_by_text("Member Summary").wait_for()
        await person.hand_back()

    script = [
        call("ask_human", reason="please look up the member"),
        call("extract", cell_right_of("Share Savings"), name="savings_balance"),
        call("done", summary="balance read"),
    ]
    result, stub, seen = await discover_with_operator(script, human)
    assert result.trace.status == "completed", result.reason
    assert result.outputs == {"savings_balance": "$2,450.17"}

    nav, field, search, balance = result.trace.actions
    assert [a.actor for a in result.trace.actions] == ["human", "human", "human", "llm"]
    assert [a.tool for a in result.trace.actions] == ["click", "fill", "click", "extract"]
    assert nav.element is not None and nav.element.frame_path == ["nav"]
    assert {loc.strategy for loc in nav.element.verified_locators} >= {"role", "text"}
    assert field.element is not None
    assert [loc.strategy for loc in field.element.verified_locators] == ["near_text", "css"]  # as the LLM's
    assert field.value == "10001"  # WHICH parameter it matched (the caller's own value), in memory only
    assert search.element is not None and search.element.accessible_name == "Search"
    assert "Member Summary" in search.page_after.headings
    assert "Member Summary" not in search.page_before.headings

    # The model was told what the human did: redacted, wrapped as untrusted.
    told = next(t for t in stub.turns if "<human_actions" in t)
    assert '<human_actions untrusted="true">' in told
    assert "click link 'Member Lookup'" in told and "click button 'Search'" in told
    assert "fill textbox 'Member Number' «redacted:len=5»" in told
    assert "10001" not in told.split("<human_actions", 1)[1].split("</human_actions>", 1)[0]

    # The typed value is nowhere: log, trace.json, operator API. The saved fill still matches its hash.
    for text in (evidence_text(result), seen["api_actions"]):
        assert "10001" not in text and "2,450.17" not in text
    saved = load_trace(result.trace_path)
    assert saved.actions[1].actor == "human" and saved.actions[1].value == saved.goal_values["member_id"]
    events = [json.loads(line) for line in (result.evidence_dir / "run.jsonl").read_text().splitlines()]
    fills = [e["details"] for e in events if e["event"] == "human_fill"]
    assert fills == [{"step": 1, "matched_parameter": True}]

    # Compiles like any trace; human steps are listed; the artifact is a draft and it replays.
    artifact = compile_trace(result.trace, CAP_ID, tenant=tenant, policy=test_policy)
    human_steps = artifact.provenance.human_assisted_steps
    assert len(human_steps) == 3 and "enter_member_id" in human_steps
    fill_step = next(s for s in artifact.steps if s.id == "enter_member_id")
    assert isinstance(fill_step.action, Fill) and fill_step.action.value == "{{inputs.member_id}}"
    assert artifact.review.status.value == "draft"
    assert "10001" not in artifact.model_dump_json()
    replayed = await execute(
        artifact, tenant, test_policy, {"member_id": "10002"}, mode="supervised", browser=browser,
        evidence_root=tmp_path,
    )  # fmt: skip
    assert replayed.status is RunStatus.success, replayed.failure
    assert replayed.outputs["savings_balance"] == "1203.55"

    # A human step whose button name is on the irreversible list compiles as irreversible.
    strict = test_policy.model_copy(
        update={"risk": test_policy.risk.model_copy(update={"irreversible_button_names": ["search"]})}
    )
    risky = compile_trace(result.trace, CAP_ID, tenant=tenant, policy=strict)
    search_step = next(s for s in risky.steps if s.id in human_steps and "search" in s.id)
    assert search_step.risk is RiskClass.irreversible
    assert all(s.risk is not RiskClass.irreversible for s in risky.steps if s is not search_step)
    assert risky.policy.requires_confirmation and risky.review.status.value == "draft"


# ---- what a human typed that is not a parameter --------------------------------------------------- #


async def test_a_typed_value_that_is_not_a_parameter_is_never_kept(
    discover_with_operator, tenant, test_policy
):
    async def human(person: Operator) -> None:
        await open_lookup_and_type(person, "55555")
        await person.main().locator("input[name=mbrno]").press("Tab")  # `change` fires on blur
        await person.hand_back()

    result, stub, seen = await discover_with_operator([call("ask_human", reason="help"), call("done")], human)
    fill = next(a for a in result.trace.actions if a.tool == "fill")
    assert fill.actor == "human" and fill.value is None  # only "did not match" is kept
    for text in (evidence_text(result), seen["api_actions"], *stub.turns, repr(result.trace)):
        assert "55555" not in text
    events = [json.loads(line) for line in (result.evidence_dir / "run.jsonl").read_text().splitlines()]
    assert [e["details"]["matched_parameter"] for e in events if e["event"] == "human_fill"] == [False]
    with pytest.raises(CompileError, match="not one of the declared parameters"):
        compile_trace(result.trace, CAP_ID, tenant=tenant, policy=test_policy)


async def test_a_human_enter_submit_is_refused_by_the_compiler(discover_with_operator, tenant, test_policy):
    async def human(person: Operator) -> None:
        await open_lookup_and_type(person, "10001")
        await person.main().locator("input[name=mbrno]").press("Enter")
        await person.main().get_by_text("Member Summary").wait_for()
        await person.hand_back()

    result, _, _ = await discover_with_operator([call("ask_human", reason="help"), call("done")], human)
    assert any(a.actor == "human" and a.tool == "press" and a.value == "Enter" for a in result.trace.actions)
    with pytest.raises(CompileError, match="Enter submitted a form"):
        compile_trace(result.trace, CAP_ID, tenant=tenant, policy=test_policy)


# ---- a stuck handoff tells the model too ---------------------------------------------------------- #


async def test_stuck_handoff_adds_the_human_summary_and_new_page_to_the_pending_turn(discover_with_operator):
    def chatty(page: str) -> Response:
        return Response([], stop_reason="end_turn")

    async def human(person: Operator) -> None:
        await person.paused()
        await person.take()
        await person.hand_back()  # nothing done

    script = [chatty] * 3 + [call("done", summary="ok")]
    result, stub, _ = await discover_with_operator(script, human)
    assert result.trace.status == "completed", result.reason
    after = next(t for t in stub.turns if "<human_actions" in t)
    assert "(no actions recorded)" in after and after.index("</human_actions>") < after.index("<page")
