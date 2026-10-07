"""Accumulates the discovery trace for the compiler, and writes it to evidence REDACTED.

In memory the trace holds the raw literals the compiler and the self-test need (typed values, goal
values, extracted outputs). On disk (`trace.json`, rewritten after every change so a crash still leaves
a trace) those become salted hashes:

    goal_values / fill values   → «<param>:sha256:…»   (same salt + value → same hash, so the compiler
                                                          can still match a fill to its parameter)
    outputs                     → «<name>:sha256:…»
    goal, reasoning, errors     → parameter values hashed, then digit runs hashed
    select options              → shape / digit redaction (UI choices, but never trust them)

Element snapshots and page states are already redacted at observation time (cua.surface.aria).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from cua.safety.redact import hash_value, page_value, redact_digit_runs
from cua.schema.trace import DiscoveryTrace, TraceAction

TRACE_FILE = "trace.json"
_TOKEN_RE = re.compile(r"(«[^»]*»)")


class TraceRecorder:
    def __init__(self, trace: DiscoveryTrace, path: Path | None = None) -> None:
        self.trace = trace
        self.path = path  # when set, the redacted trace is rewritten after every change
        self._flush()

    # ---- recording ------------------------------------------------------------------------------- #

    def add(self, action: TraceAction) -> None:
        self.trace.actions.append(action)
        self._flush()

    def output(self, name: str, value: str) -> None:
        """An extracted value. Raw in memory (for the self-test), hashed on disk."""
        self.trace.outputs[name] = value
        self._flush()

    def finish(self, status: Literal["completed", "escalated", "failed"]) -> None:
        self.trace.status = status
        self._flush()

    @property
    def next_seq(self) -> int:
        return len(self.trace.actions) + 1

    # ---- persistence ----------------------------------------------------------------------------- #

    def redacted(self) -> DiscoveryTrace:
        """The evidence-safe copy of the trace. The in-memory trace is not modified."""
        params = self.trace.goal_values

        def scrub(text: str | None) -> str | None:
            if text is None:
                return None
            for name, value in params.items():
                if value:
                    text = text.replace(value, hash_value(value, name))
            # Hash remaining numbers, but never re-hash the hex inside an existing «…» token.
            return "".join(
                part if _TOKEN_RE.fullmatch(part) else redact_digit_runs(part)
                for part in _TOKEN_RE.split(text)
            )

        def param_hash(value: str) -> str:
            name = next((n for n, v in params.items() if v == value), "value")
            return hash_value(value, name)

        actions = []
        for a in self.trace.actions:
            value = a.value
            if value is not None:
                if a.tool == "fill":
                    value = param_hash(value)
                elif a.tool == "select":
                    value = scrub(page_value(value))
            actions.append(
                a.model_copy(
                    update={"value": value, "reasoning": scrub(a.reasoning), "error": scrub(a.error)}
                )
            )
        return self.trace.model_copy(
            update={
                "goal": scrub(self.trace.goal),
                "goal_values": {n: hash_value(v, n) for n, v in params.items()},
                "outputs": {n: hash_value(v, n) for n, v in self.trace.outputs.items()},
                "actions": actions,
            }
        )

    def save(self, path: Path) -> Path:
        """Write the REDACTED trace atomically (temp file + rename): a reader never sees half a file."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.redacted().model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _flush(self) -> None:
        if self.path is not None:
            self.save(self.path)


def load_trace(path: Path) -> DiscoveryTrace:
    """A saved (redacted) trace. Fill values and goal values are hashes that still match each other."""
    return DiscoveryTrace.model_validate_json(path.read_text(encoding="utf-8"))
