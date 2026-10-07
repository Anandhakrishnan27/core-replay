"""Redaction applied to everything written to logs/evidence. Artifacts hold templates, never values."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from cua.schema.artifact import Sensitivity

_DEFAULT_SALT = "dev-only-salt"


def _salt() -> str:
    return os.getenv("CUA_REDACTION_SALT", _DEFAULT_SALT)


def hash_value(value: str, label: str) -> str:
    digest = hashlib.sha256((_salt() + value).encode()).hexdigest()[:10]
    return f"«{label}:sha256:{digest}»"


def redact(value: Any, sensitivity: Sensitivity, label: str = "value") -> Any:
    """Return a log-safe representation of `value`."""
    if value is None:
        return None
    if sensitivity is Sensitivity.secret:
        return f"«secret:{label}»"
    if sensitivity is Sensitivity.pii:
        return hash_value(str(value), label)
    return value


def redact_typed(value: str) -> str:
    """For human-typed values captured during handoff: keep only the length."""
    return f"«redacted:len={len(value)}»"


_NUMBER_RE = re.compile(r"\d(?:[\d,.]*\d)?")


def redact_digit_runs(text: str, min_digits: int = 4) -> str:
    """Hash every number with >= min_digits digits (`,` / `.` count as part of it, so `$97.40` is hashed)."""

    def sub(m: re.Match[str]) -> str:
        run = m.group(0)
        return hash_value(run, "digits") if sum(c.isdigit() for c in run) >= min_digits else run

    return _NUMBER_RE.sub(sub, text)


def redact_mapping(data: dict[str, Any], sensitivity_by_key: dict[str, Sensitivity]) -> dict[str, Any]:
    """Redact known keys; unknown keys default to `internal` (kept)."""
    return {k: redact(v, sensitivity_by_key.get(k, Sensitivity.internal), k) for k, v in data.items()}


# ---- page values seen during discovery ------------------------------------------------------------ #
# Data on screen (balances, ids, dates) is replaced by its SHAPE before it reaches the LLM or the trace.
# The compiler derives parse types and MatchRule.text_pattern from the shape; the value is never kept.

MASKED = "«masked»"
_GROUPED = r"\d{1,3}(?:,?\d{3})*"
_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("date", re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")),
    ("currency", re.compile(rf"^\(?-?[$€£]\s?-?{_GROUPED}(?:\.\d{{2}})?\)?$")),
    ("decimal", re.compile(rf"^-?{_GROUPED}\.\d+$")),
    ("integer", re.compile(rf"^-?{_GROUPED}$")),
]
_LONG_DIGITS_RE = re.compile(r"\d{4,}|\d(?:[,.]?\d){3,}")


def shape_token(kind: str) -> str:
    return f"«shape:{kind}»"


def data_shape(text: str) -> str | None:
    """'currency' | 'decimal' | 'integer' | 'date' for a value-only text; 'text' if it holds a long number."""
    t = " ".join(text.split())
    for kind, pattern in _SHAPES:
        if pattern.match(t):
            return kind
    return "text" if _LONG_DIGITS_RE.search(t) else None


def page_value(text: str, *, masked: bool = False) -> str:
    """Log- and LLM-safe form of one element's text: «masked», «shape:kind», or the UI text unchanged."""
    if masked:
        return MASKED
    kind = data_shape(text)
    return text if kind is None else shape_token(kind)
