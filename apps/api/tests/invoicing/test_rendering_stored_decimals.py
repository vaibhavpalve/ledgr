"""Invoice rendering of values as the DATABASE returns them (found while building SI-16).

`sales_invoice_line` stores quantity, unit price and discount at four places and VAT rates at
three, so a line read back carries `10.0000` where a test typically builds `10`. The formatter
refuses to show a value at fewer places than it carries (rounding would print a different
figure), and rendering used a fixed scale of two - so issuing any invoice read from the
database failed at the PDF. Every rendering test built its decimals by hand, so none saw it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.invoicing.rendering import _number, _price, _trimmed

D = Decimal


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("10", "10,00"),
        ("10.0000", "10,00"),  # what the database returns for 10
        ("2.5000", "2,50"),
        ("0.5", "0,50"),
        ("1.2500", "1,25"),
        ("3.1250", "3,125"),  # a real third place is kept, never rounded
        ("1234567.0000", "1.234.567,00"),
        ("-4.0000", "-4,00"),
        ("21.000", "21,00"),  # a VAT rate as stored
        ("0.0000", "0,00"),
    ],
)
def test_a_number_is_shown_at_its_own_scale_never_rounded(stored: str, shown: str) -> None:
    assert _number(D(stored), "nl-NL") == shown


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("95", "€\u00a095,00"),
        ("95.0000", "€\u00a095,00"),
        ("49.9500", "€\u00a049,95"),
        ("0.0350", "€\u00a00,035"),  # a price finer than a cent keeps its precision
        ("-10.0000", "€\u00a0-10,00"),
    ],
)
def test_a_unit_price_is_shown_at_its_own_scale_with_the_currency(stored: str, shown: str) -> None:
    assert _price(D(stored), "nl-NL") == shown


def test_trimming_changes_the_scale_but_never_the_value() -> None:
    for text in ("10.0000", "0.0350", "3.1250", "21.000", "1E+3", "0.10"):
        value = D(text)
        trimmed, _ = _trimmed(value)
        assert trimmed == value
