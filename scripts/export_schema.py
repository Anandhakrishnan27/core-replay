"""Regenerate capabilities/artifact.schema.json from the Pydantic models."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cua.schema.artifact import CapabilityArtifact  # noqa: E402

out = Path(__file__).resolve().parents[1] / "capabilities" / "artifact.schema.json"
out.write_text(json.dumps(CapabilityArtifact.model_json_schema(), indent=2) + "\n")
print(f"wrote {out}")
