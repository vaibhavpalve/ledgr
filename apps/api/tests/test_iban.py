"""api.iban - SI-02's checksum gate on the supplier's own bank account.

A wrong IBAN here does not fail loudly: it ends up on a customer-scanned QR
code (see api.invoicing.epc_qr), silently pointing a payment at the wrong
account or none at all. These tests exist to make that class of mistake
fail a test instead.
"""

from __future__ import annotations

import pytest

from api.iban import Iban, normalise, parse

#: Real, checksum-valid IBANs (public examples from each country's central
#: bank / Wikipedia's IBAN article) - not fabricated, since a fabricated one
#: could easily fail for the wrong reason (right shape, wrong checksum).
VALID = [
    "NL91ABNA0417164300",
    "DE89370400440532013000",
    "BE68539007547034",
    "FR1420041010050500013M02606",
    "GB29NWBK60161331926819",
]


@pytest.mark.parametrize("value", VALID)
def test_valid_ibans_parse(value: str) -> None:
    parsed = parse(value)
    assert parsed is not None, value
    assert parsed.value == value
    assert str(parsed) == value


@pytest.mark.parametrize("value", VALID)
def test_whitespace_and_separators_are_tolerated(value: str) -> None:
    """Bank websites and PDFs format IBANs with spaces every four characters
    (and sometimes dots or dashes); a supplier pasting one in from their own
    bank statement should not be rejected for that formatting alone.
    """
    spaced = " ".join(value[i : i + 4] for i in range(0, len(value), 4))
    assert parse(spaced) == parse(value)
    assert parse(spaced.lower()) == parse(value)
    assert parse(f"  {spaced}  ") == parse(value)


@pytest.mark.parametrize(
    "value",
    [
        "NL91ABNA0417164301",  # last digit flipped - fails the mod-97 checksum
        "NL91ABNA0417164399",
        "DE89370400440532013001",
    ],
)
def test_checksum_broken_ibans_are_rejected(value: str) -> None:
    """The shape (country code, check digits, BBAN length) is right; only the
    mod-97 checksum is wrong. A shape-only check would let these through.
    """
    assert parse(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not an iban",
        "NL91",  # too short
        "1191ABNA0417164300",  # country code must be letters
        "NLXXABNA0417164300",  # check digits must be digits
        "NL91ABNA0417164300" + "0" * 20,  # exceeds max IBAN length (34)
    ],
)
def test_malformed_input_is_rejected(value: str) -> None:
    assert parse(value) is None


def test_none_is_none() -> None:
    """A supplier without an IBAN on file is ordinary - no QR code is
    rendered for that invoice (see epc_qr, rendering) - so this is a value,
    not an error.
    """
    assert parse(None) is None


def test_normalise_never_raises_on_anything() -> None:
    for junk in ("", "   ", "???", "\n\t", "-.-.-", "🙂"):
        assert isinstance(normalise(junk), str)


def test_normalise_is_idempotent() -> None:
    for value in VALID:
        once = normalise(value)
        assert normalise(once) == once


def test_country_code_is_the_first_two_characters() -> None:
    parsed = parse("NL91ABNA0417164300")
    assert parsed is not None
    assert parsed.country_code == "NL"


def test_iban_is_frozen() -> None:
    parsed = parse("NL91ABNA0417164300")
    assert parsed is not None
    with pytest.raises(AttributeError):
        parsed.value = "DE89370400440532013000"  # type: ignore[misc]


def test_equal_value_ibans_compare_equal() -> None:
    assert Iban(value="NL91ABNA0417164300") == Iban(value="NL91ABNA0417164300")
