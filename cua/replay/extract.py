"""Parse extracted UI text into contract types. Unparseable text is a hard failure, never garbage."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY = re.compile(r"^\(?-?\$?\s*-?[0-9][0-9,]*(\.[0-9]+)?\)?$")


class ParseError(ValueError):
    pass


def parse_value(text: str, kind: str) -> str | int | Decimal | date:
    raw = text.strip()
    if kind == "text":
        return raw
    if kind == "integer":
        try:
            return int(raw.replace(",", ""))
        except ValueError as e:
            raise ParseError(f"expected integer, observed {raw!r}") from e
    if kind in ("decimal", "currency"):
        if kind == "currency" and not _CURRENCY.match(raw):
            raise ParseError(f"expected currency, observed {raw!r}")
        negative = raw.startswith("(") and raw.endswith(")") or "-" in raw
        digits = re.sub(r"[^0-9.]", "", raw)
        try:
            value = Decimal(digits)
        except InvalidOperation as e:
            raise ParseError(f"expected {kind}, observed {raw!r}") from e
        return -value if negative else value
    if kind == "date":
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise ParseError(f"expected date, observed {raw!r}")
    raise ParseError(f"unknown parse kind {kind!r}")
