from datetime import date
from decimal import Decimal

import pytest

from cua.replay.extract import ParseError, parse_value


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$2,450.17", Decimal("2450.17")),
        ("1,203.55", Decimal("1203.55")),
        ("-$15.00", Decimal("-15.00")),
        ("($15.00)", Decimal("-15.00")),
    ],
)
def test_currency(text, expected):
    assert parse_value(text, "currency") == expected


@pytest.mark.parametrize("bad", ["N/A", "", "Active", "$--"])
def test_currency_rejects_garbage(bad):
    with pytest.raises(ParseError):
        parse_value(bad, "currency")


def test_other_kinds():
    assert parse_value(" Active ", "text") == "Active"
    assert parse_value("1,200", "integer") == 1200
    assert parse_value("10/01/2026", "date") == date(2026, 10, 1)
