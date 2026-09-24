"""ADR-089: CSV a Dutch Excel opens as columns of numbers."""

from __future__ import annotations

from decimal import Decimal

from api.exports.csv import render


def test_dutch_dialect_is_semicolons_decimal_commas_and_a_bom() -> None:
    body = render(
        ["rekening", "debet"], [["1100", Decimal("12500.5")], ["Privé; stortingen", Decimal("0")]]
    )
    assert body.startswith(chr(0xFEFF))
    assert body[1:].split("\r\n") == [
        "rekening;debet",
        "1100;12500,50",
        '"Privé; stortingen";0,00',
        "",
    ]


def test_international_dialect_is_plain_csv() -> None:
    body = render(["account", "debit"], [["1100", Decimal("-3.1")]], dialect="international")
    assert body == "account,debit\r\n1100,-3.10\r\n"


def test_no_thousands_separator_is_ever_written() -> None:
    assert "1234567,89" in render(["x"], [[Decimal("1234567.89")]])
