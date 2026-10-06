"""Capability catalog on disk: capabilities/<product>/<domain.verb_noun>/<semver>.json"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cua.config import CAPABILITIES_DIR
from cua.schema.artifact import CapabilityArtifact


def _semver_key(p: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in p.stem.split("."))


def path_for(capability_id: str, version: str | None = None, root: Path = CAPABILITIES_DIR) -> Path:
    product, rest = capability_id.split(".", 1)
    folder = root / product / rest
    if version:
        return folder / f"{version}.json"
    versions = sorted(folder.glob("*.json"), key=_semver_key)
    if not versions:
        raise FileNotFoundError(f"no versions of {capability_id} in {folder}")
    return versions[-1]


def load(capability_id: str, version: str | None = None, root: Path = CAPABILITIES_DIR) -> CapabilityArtifact:
    return load_file(path_for(capability_id, version, root))


def load_file(path: Path) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(path.read_text())


def save(artifact: CapabilityArtifact, root: Path = CAPABILITIES_DIR) -> Path:
    path = path_for(artifact.id, artifact.version, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.model_dump_json(indent=2, exclude_none=True) + "\n")
    return path


def approve(
    capability_id: str, reviewer: str, version: str | None = None, root: Path = CAPABILITIES_DIR
) -> Path:
    art = load(capability_id, version, root)
    data = art.model_dump(mode="json")
    data["review"] = {
        "status": "approved",
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "notes": None,
    }
    return save(CapabilityArtifact.model_validate(data), root)


def list_all(root: Path = CAPABILITIES_DIR) -> list[dict[str, str]]:
    rows = []
    for p in sorted(root.glob("*/*/*.json")):
        d = json.loads(p.read_text())
        rows.append(
            {
                "id": d["id"],
                "version": d["version"],
                "status": d.get("review", {}).get("status", "draft"),
                "title": d["title"],
            }
        )
    return rows
