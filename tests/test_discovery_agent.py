"""Discovery agent loop against the live mock bank, driven by a SCRIPTED stub client (no LLM, no API key).

The stub plays the model's part with a fixed list of tool calls, picking refs from the page text it is
sent. This tests the harness: request shape, tool execution through Surface, guards, stuck detection,
handoff and the recorded trace. It does not test the model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from cua.config import Credentials
from cua.discovery.agent import FALLBACK_BETA, DiscoveryResult, action_risk, discover
from cua.discovery.tools import TOOLS
from cua.safety.policy import PolicyGate
from cua.schema.artifact import RiskClass
from cua.schema.trace import ElementSnapshot

GOAL = "look up member 10001 and read the savings balance"
PARAMS = {"member_id": "10001"}

# ---- stub client -------------------------------------------------------------------------------- #


@dataclass
class Block:
    type: str
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "tool_use"
    usage: Usage = field(default_factory=Usage)
    model: str = "stub"
    stop_details: Any = None


Step = Callable[[str], Response]


def tool(tool_name: str, /, **args: Any) -> Response:
    return Response(
        [Block("text", text="next step"), Block("tool_use", id=f"tu_{tool_name}", name=tool_name, input=args)]
    )


def call(tool_name: str, pick: Callable[[str], dict[str, Any]] | None = None, /, **args: Any) -> Step:
    """A scripted turn: `pick(page_text)` supplies arguments that depend on the page (refs)."""
    return lambda page: tool(tool_name, **(pick(page) if pick else {}), **args)


class StubMessages:
    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.pages: list[str] = []  # page text the "model" was shown, per request

    async def create(self, **kwargs: Any) -> Response:
        self.requests.append({k: v for k, v in kwargs.items() if k != "messages"})
        page = latest_page(kwargs["messages"])
        self.pages.append(page)
        if not self.script:
            return tool("done", summary="script finished")
        return self.script.pop(0)(page)


def latest_page(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg["role"] != "user":
            continue
        content = msg["content"]
        texts = (
            [content] if isinstance(content, str) else [b["text"] for b in content if b.get("type") == "text"]
        )
        for text in texts:
            if "<page" in text:
                return text
    return ""


def ref_of(role: str, name: str) -> Callable[[str], dict[str, Any]]:
    def pick(page: str) -> dict[str, Any]:
        m = re.search(rf"- {role} {re.escape(json.dumps(name))}[^\n]*\[ref=([^\]]+)\]", page)
        assert m, f"{role} {name!r} not on the page"
        return {"ref": m.group(1)}

    return pick


def first_ref(role: str) -> Callable[[str], dict[str, Any]]:
    def pick(page: str) -> dict[str, Any]:
        m = re.search(rf"- {role}\b[^\n]*\[ref=([^\]]+)\]", page)
        assert m, f"no {role} on the page"
        return {"ref": m.group(1)}

    return pick


def cell_right_of(label: str) -> Callable[[str], dict[str, Any]]:
    """The cell after the one labelled `label` in the same row (e.g. the balance next to 'Share Savings')."""

    def pick(page: str) -> dict[str, Any]:
        m = re.search(
            rf"- cell {re.escape(json.dumps(label))} \[ref=[^\]]+\]\n\s*- cell [^\n]*\[ref=([^\]]+)\]", page
        )
        assert m, f"no cell right of {label!r}"
        return {"ref": m.group(1)}

    return pick


HAPPY_PATH: list[Step] = [
    call("click", ref_of("link", "Member Lookup")),
    call("fill", first_ref("textbox"), value="10001"),
    call("click", ref_of("button", "Search")),
    call("extract", cell_right_of("Share Savings"), name="savings_balance"),
    call("done", summary="balance read"),
]


@pytest.fixture
def run(browser, tenant, test_policy, tmp_path):
    async def go(
        script: list[Step],
        *,
        fault: str | None = None,
        model: str = "claude-opus-5-5",
        policy=None,
        tenant_=None,
    ):
        stub = StubMessages(script)
        result = await discover(
            GOAL,
            tenant_ or tenant,
            policy or test_policy,
            PARAMS,
            messages_api=stub,
            model=model,
            fault=fault,
            browser=browser,
            evidence_root=tmp_path,
        )
        return result, stub

    return go


def log_text(result: DiscoveryResult) -> str:
    return (result.evidence_dir / "run.jsonl").read_text()


# ---- happy path --------------------------------------------------------------------------------- #


async def test_happy_path_records_trace_and_keeps_values_out_of_evidence(run):
    result, stub = await run(HAPPY_PATH)
    assert result.trace.status == "completed", result.reason
    assert [a.tool for a in result.trace.actions] == ["click", "fill", "click", "extract"]
    assert all(a.ok for a in result.trace.actions)
    assert result.outputs == {"savings_balance": "$2,450.17"}  # raw, in memory only

    nav, field_, search, balance = result.trace.actions
    assert nav.element is not None and nav.element.frame_path == ["nav"]
    assert {loc.strategy for loc in nav.element.verified_locators} >= {"role", "text", "css"}
    # Legacy form: no <label for>, so the caption-as-label candidate fails verification; near_text holds.
    assert field_.element is not None
    assert [loc.strategy for loc in field_.element.verified_locators] == ["near_text", "css"]
    assert field_.value == "10001"  # raw in memory; redacted when the recorder saves (stage 3)
    assert (
        "Member Summary" in search.page_after.headings and "Member Summary" not in search.page_before.headings
    )
    assert balance.element is not None and balance.output_name == "savings_balance"
    assert [loc.strategy for loc in balance.element.verified_locators][0] == "table_cell"

    # The model never saw a member number, name or balance after typing.
    for page in stub.pages:
        assert "2,450.17" not in page and "Test Member A" not in page
    shown = "".join(p.split("<page", 1)[1] for p in stub.pages[3:])  # page part only, not the goal
    assert "10001" not in shown  # after the search the member number cell is masked
    # Nothing raw in the run log.
    log = log_text(result)
    assert "10001" not in log and "2,450.17" not in log and "Test Member A" not in log
    assert list((result.evidence_dir / "steps").glob("*_click.png"))


async def test_request_shape_matches_the_documented_api(run):
    _, stub = await run(HAPPY_PATH[:1])
    req = stub.requests[0]
    assert req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "medium"}
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert "thinking" not in req  # model default (adaptive)
    assert req["tools"] is TOOLS
    for t in TOOLS:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


async def test_no_fallbacks_for_models_the_docs_do_not_cover(run):
    _, stub = await run(HAPPY_PATH[:1], model="claude-haiku-4-5")
    assert "fallbacks" not in stub.requests[0] and "betas" not in stub.requests[0]


async def test_history_is_append_only_with_tool_results_before_the_page(run):
    result, stub = await run(HAPPY_PATH[:2])
    assert result.trace.status == "completed"
    assert len(stub.requests) == 3  # click, fill, then "done" from the exhausted script


# ---- guards ------------------------------------------------------------------------------------- #


async def test_fill_with_a_value_not_in_the_goal_is_refused(run):
    script = [
        call("click", ref_of("link", "Member Lookup")),
        call("fill", first_ref("textbox"), value="99999"),
    ]
    result, _ = await run(script)
    assert [a.tool for a in result.trace.actions] == ["click"]  # nothing typed, nothing recorded
    assert "99999" not in log_text(result)


async def test_enter_is_refused_so_submit_risk_stays_known(run):
    script = [call("click", ref_of("link", "Member Lookup")), call("press", key="Enter")]
    result, _ = await run(script)
    assert [a.tool for a in result.trace.actions] == ["click"]


async def test_unknown_ref_is_a_tool_error_not_a_crash(run):
    result, _ = await run([call("click", ref="f9e999")])
    assert result.trace.status == "completed" and result.trace.actions == []


async def test_bad_output_name_is_refused(run):
    result, _ = await run([call("extract", ref_of("link", "Member Lookup"), name="Savings Balance")])
    assert result.trace.actions == [] and result.outputs == {}


def test_irreversible_button_names_are_classified_before_acting(test_policy):
    gate = PolicyGate(test_policy)
    submit = ElementSnapshot(tag="input", role="button", accessible_name="Submit Transfer")
    search = ElementSnapshot(tag="input", role="button", accessible_name="Search")
    assert action_risk("click", submit, gate) is RiskClass.irreversible
    assert action_risk("click", search, gate) is RiskClass.read
    assert action_risk("fill", search, gate) is RiskClass.reversible


# ---- interstitials, escalation, failures -------------------------------------------------------- #


async def test_dismissed_notice_is_recorded_as_dismiss(run):
    script = [
        call("click", ref_of("link", "Member Lookup")),
        call("dismiss", ref_of("button", "OK")),
        call("fill", first_ref("textbox"), value="10001"),
    ]
    result, _ = await run(script, fault="notice")
    assert [a.tool for a in result.trace.actions] == ["click", "dismiss", "fill"]
    dismiss = result.trace.actions[1]
    assert "System Notice" in dismiss.page_before.headings
    assert "System Notice" not in dismiss.page_after.headings


async def test_ask_human_without_an_operator_escalates(run):
    result, _ = await run([call("ask_human", reason="unexpected screen for member 10001")])
    assert result.trace.status == "escalated"
    log = log_text(result)
    assert '"handoff_requested"' in log and "10001" not in log
    assert list((result.evidence_dir / "steps").glob("*_handoff.png"))


async def test_turns_without_a_tool_call_end_as_stuck(run):
    def chatty(page: str) -> Response:
        return Response([Block("text", text="I think I should look around.")], stop_reason="end_turn")

    result, stub = await run([chatty, chatty, chatty])
    assert result.trace.status == "escalated"
    assert len(stub.requests) == 3  # stuck after the third turn (same screen / failed turns)


async def test_step_budget_escalates(run, test_policy):
    policy = test_policy.model_copy(
        update={"limits": test_policy.limits.model_copy(update={"discovery_max_steps": 2})}
    )
    script = [call("click", ref_of("link", "Home")), call("click", ref_of("link", "Member Lookup"))]
    result, _ = await run(script, policy=policy)
    assert result.trace.status == "escalated" and "step budget" in result.reason


async def test_refusal_fails_the_run(run):
    def refuse(page: str) -> Response:
        return Response([], stop_reason="refusal", stop_details=type("D", (), {"category": "cyber"})())

    result, _ = await run([refuse])
    assert result.trace.status == "failed" and "refused" in result.reason


async def test_api_error_after_retries_fails_the_run(run):
    def boom(page: str) -> Response:
        raise anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    result, _ = await run([boom])
    assert result.trace.status == "failed" and "APIConnectionError" in result.reason


async def test_login_failure_fails_before_any_model_call(run, tenant):
    unset = Credentials(username_env="CUA_TEST_UNSET_USER", password_env="CUA_TEST_UNSET_PASSWORD")
    result, stub = await run(HAPPY_PATH, tenant_=tenant.model_copy(update={"credentials": unset}))
    assert result.trace.status == "failed" and "login failed" in result.reason
    assert stub.requests == []


def test_no_anthropic_import_outside_discovery():
    root = Path(__file__).resolve().parents[1] / "cua"
    offenders = [
        p
        for p in root.rglob("*.py")
        if "discovery" not in p.parts and re.search(r"^\s*(import|from) anthropic", p.read_text(), re.M)
    ]
    assert offenders == []
