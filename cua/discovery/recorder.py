"""Accumulates the discovery trace for the compiler."""

from __future__ import annotations

from pathlib import Path

from cua.schema.trace import DiscoveryTrace, TraceAction


class TraceRecorder:
    def __init__(self, trace: DiscoveryTrace) -> None:
        self.trace = trace

    def add(self, action: TraceAction) -> None:
        self.trace.actions.append(action)

    def save(self, path: Path) -> Path:
        path.write_text(self.trace.model_dump_json(indent=2), encoding="utf-8")
        return path
