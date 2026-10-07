"""Structured run log (JSONL) + evidence folder layout. Written DURING the run (crash-safe)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

Mode = Literal["discovery", "replay"]


class RunLogger:
    """One instance per run. Callers must pass already-redacted details (see cua.safety.redact)."""

    def __init__(self, evidence_root: Path, run_id: str, mode: Mode) -> None:
        self.run_id = run_id
        self.mode = mode
        self.dir = evidence_root / f"{mode}_{run_id}"
        (self.dir / "steps").mkdir(parents=True, exist_ok=True)
        self._log = self.dir / "run.jsonl"

    def event(self, event: str, step_id: str | None = None, **details: Any) -> None:
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "mode": self.mode,
            "step_id": step_id,
            "event": event,
            "details": details,
        }
        with self._log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def write_json(self, name: str, model: BaseModel | dict[str, Any]) -> Path:
        path = self.dir / name
        data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.dir))
