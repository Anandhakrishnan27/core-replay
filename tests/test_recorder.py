"""Trace recorder: raw in memory, redacted on disk, rewritten after every change."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cua.discovery.recorder import TraceRecorder, load_trace
from cua.safety.redact import hash_value
from cua.schema.trace import DiscoveryTrace, ElementSnapshot, PageState, TraceAction

PAGE = PageState(url="http://127.0.0.1/console", title="MockBank Core", headings=["Member Lookup"])


def make_trace() -> DiscoveryTrace:
    return DiscoveryTrace(
        run_id="r1",
        goal="look up member 10001 (Jane Q Public) and read the savings balance",
        goal_values={"member_id": "10001", "member_name": "Jane Q Public"},
        tenant_id="cu_alpha",
        model="stub",
        started_at=datetime.now(UTC),
    )


def action(seq: int, tool: str, **kw) -> TraceAction:
    return TraceAction(
        seq=seq,
        at=datetime.now(UTC),
        actor="llm",
        tool=tool,
        element=ElementSnapshot(tag="input", role="textbox", frame_path=["main"]),
        page_before=PAGE,
        page_after=PAGE,
        **kw,
    )


def record_run(path: Path | None = None) -> TraceRecorder:
    rec = TraceRecorder(make_trace(), path)
    rec.add(action(1, "fill", value="10001", reasoning="Type member 10001 for Jane Q Public"))
    rec.add(action(2, "fill", value="Jane Q Public"))
    rec.add(action(3, "select", value="Acct 12345678"))
    rec.add(action(4, "press", value="Tab"))
    rec.add(action(5, "click", ok=False, error="click on 'f3e9' failed: member 10001 locked"))
    rec.output("savings_balance", "$2,450.17")
    return rec


RAW = ["10001", "Jane Q Public", "2,450.17", "12345678"]


def test_memory_keeps_raw_values_for_the_compiler():
    rec = record_run()
    assert rec.trace.actions[0].value == "10001"
    assert rec.trace.outputs == {"savings_balance": "$2,450.17"}
    assert rec.next_seq == 6


def test_saved_trace_has_no_raw_values(tmp_path):
    path = record_run().save(tmp_path / "trace.json")
    text = path.read_text()
    for raw in RAW:
        assert raw not in text, raw
    assert "«member_id:sha256:" in text and "«savings_balance:sha256:" in text


def test_fill_hash_still_matches_its_parameter_on_disk(tmp_path):
    saved = load_trace(record_run().save(tmp_path / "trace.json"))
    fill_id, fill_name = saved.actions[0].value, saved.actions[1].value
    assert fill_id == saved.goal_values["member_id"] == hash_value("10001", "member_id")
    assert fill_name == saved.goal_values["member_name"]


def test_free_text_is_scrubbed_without_mangling_hash_tokens(tmp_path):
    saved = load_trace(record_run().save(tmp_path / "trace.json"))
    reasoning = saved.actions[0].reasoning or ""
    assert hash_value("10001", "member_id") in reasoning  # hashed once, hex left intact
    assert hash_value("Jane Q Public", "member_name") in reasoning
    assert saved.actions[2].value is not None and "«" in saved.actions[2].value  # select option scrubbed
    assert saved.actions[3].value == "Tab"  # keys are not data
    assert hash_value("10001", "member_id") in (saved.actions[4].error or "")
    assert hash_value("10001", "member_id") in saved.goal


def test_trace_file_is_rewritten_after_every_change(tmp_path):
    path = tmp_path / "trace.json"
    rec = TraceRecorder(make_trace(), path)
    assert load_trace(path).actions == []  # exists from the start: a crash still leaves a trace
    rec.add(action(1, "click"))
    assert len(load_trace(path).actions) == 1
    rec.finish("escalated")
    assert load_trace(path).status == "escalated"
    assert not list(tmp_path.glob("*.tmp"))  # atomic replace leaves no temp file


def test_redaction_does_not_touch_the_in_memory_trace(tmp_path):
    rec = record_run(tmp_path / "trace.json")
    assert rec.trace.goal_values["member_id"] == "10001"
    assert rec.trace.actions[0].value == "10001"
