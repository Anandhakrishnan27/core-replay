"""Compiler pass: conditions. The runtime error taxonomy of a compiled capability comes from three places:

  1. the product's condition pack, a reviewable data file: config/products/<product>.conditions.yaml
     (session expiry, errors, interstitials this product shows whatever the capability)
  2. derived business outcomes: a value read from a table row that is missing while its screen is
     showing (e.g. no 'Share Savings' row) → NO_<ROW>, a legitimate answer, not an error
  3. interstitials the model dismissed during discovery that the pack does not already cover
Order is business → recoverable → hard failure (the race evaluates conditions in declared order).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cua.compiler import CompileError
from cua.compiler.checkpoints import stable_text
from cua.compiler.locators import build_target, row_anchor, slug
from cua.config import CONFIG_DIR
from cua.schema.artifact import (
    Check,
    ConditionClass,
    Dismiss,
    KnownCondition,
    ReturnOutcome,
    Target,
    TargetAbsent,
    TextPresent,
)
from cua.schema.trace import TraceAction

PACK_DIR = CONFIG_DIR / "products"
_ORDER = {ConditionClass.business_outcome: 0, ConditionClass.recoverable: 1, ConditionClass.hard_failure: 2}
DERIVED_TIMEOUT_MS = 1_500


@dataclass
class Pack:
    product: str
    targets: dict[str, Any] = field(default_factory=dict)  # raw Target dicts
    conditions: dict[str, Any] = field(default_factory=dict)  # raw KnownCondition dicts (+ placeholders)


def load_pack(product: str, pack_dir: Path = PACK_DIR) -> Pack:
    """The product's pack, or an empty one (a product without a pack gets derived conditions only)."""
    path = pack_dir / f"{product}.conditions.yaml"
    if not path.exists():
        return Pack(product=product)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("product") != product:
        raise CompileError(f"{path.name}: declares product {raw.get('product')!r}, expected {product!r}")
    return Pack(product=product, targets=raw.get("targets") or {}, conditions=raw.get("conditions") or {})


@dataclass
class Resolved:
    conditions: dict[str, KnownCondition]
    targets: dict[str, Target]


def resolve_pack(pack: Pack, *, entity: str | None, search_step: str | None) -> Resolved:
    """Pack entries → KnownConditions for this capability; only the targets they use come along."""
    conditions: dict[str, KnownCondition] = {}
    for cid, raw in pack.conditions.items():
        applies = raw.get("applies_to", "all")
        if applies == "search_submit":
            if not (search_step and entity):
                continue  # no search in this capability: the condition cannot apply
            applies = [search_step]
        text = json.dumps({**raw, "applies_to": applies})
        if "{entity}" in text + cid or "{ENTITY}" in text + cid:
            if not entity:
                continue
            text = text.replace("{entity}", entity).replace("{ENTITY}", entity.upper())
            cid = cid.replace("{entity}", entity)
        try:
            conditions[cid] = KnownCondition.model_validate(json.loads(text))
        except ValueError as e:
            raise CompileError(f"{pack.product} pack, condition '{cid}': {str(e).splitlines()[0]}") from None
    used = {c.handler.target for c in conditions.values() if isinstance(c.handler, Dismiss)}
    targets: dict[str, Target] = {}
    for tid in used:
        if tid not in pack.targets:
            raise CompileError(f"{pack.product} pack: condition uses undefined target '{tid}'")
        targets[tid] = Target.model_validate(pack.targets[tid])
    return Resolved(conditions=conditions, targets=targets)


def missing_row(
    step_id: str, heading: str, cell_target: str, cell: TraceAction
) -> tuple[str, str, KnownCondition]:
    """(condition id, outcome code, condition): the screen is up but the row the value lives in is not."""
    anchor = row_anchor(cell.element) if cell.element else None
    if not anchor:
        raise CompileError(f"action {cell.seq}: a table value without a row label")
    code = "NO_" + slug(anchor).upper()
    condition = KnownCondition(
        classification=ConditionClass.business_outcome,
        description=f"'{heading}' is shown but has no '{anchor}' row",
        detect=Check(
            all_of=[TextPresent(text=heading), TargetAbsent(target=cell_target)],
            timeout_ms=DERIVED_TIMEOUT_MS,
        ),
        handler=ReturnOutcome(outcome=code),
        applies_to=[step_id],
    )
    return f"no_{slug(anchor)}", code, condition


def dialog_text(dismiss: TraceAction) -> str | None:
    """The heading that was on screen before the dismiss and gone after it (e.g. 'System Notice')."""
    after = set(dismiss.page_after.headings)
    return next((h for h in dismiss.page_before.headings if h not in after and stable_text(h)), None)


def observed_interstitial(dismiss: TraceAction) -> tuple[str, str, Target, KnownCondition] | None:
    """(condition id, target id, target, condition) for a dialog the model dismissed, or None."""
    text = dialog_text(dismiss)
    if text is None or dismiss.element is None:
        return None
    cid = slug(text)
    target_id = f"{cid}_close"
    # Interstitials can open in any frame: empty frame_path = top document and every frame, exactly one.
    target = build_target(dismiss.element, frame_path=[], where=f"dismiss of '{text}'")
    condition = KnownCondition(
        classification=ConditionClass.recoverable,
        description=f"'{text}' interstitial (dismissed during discovery)",
        detect=Check(any_of=[TextPresent(text=text)], timeout_ms=500),
        handler=Dismiss(target=target_id, max_times=2),
    )
    return cid, target_id, target, condition


def covered_texts(conditions: dict[str, KnownCondition]) -> set[str]:
    return {p.text for c in conditions.values() for p in c.detect.predicates() if isinstance(p, TextPresent)}


def ordered(conditions: dict[str, KnownCondition]) -> dict[str, KnownCondition]:
    return dict(sorted(conditions.items(), key=lambda kv: _ORDER[kv[1].classification]))
