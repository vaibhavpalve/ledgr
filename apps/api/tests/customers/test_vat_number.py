"""api.customers.vat_number - FR-ONB-003's syntax half.

The case table is in vat_number_cases.py. These tests are the properties that
are not one row of it.
"""

from __future__ import annotations

import pytest

from api.customers.vat_number import (
    EU_VAT_COUNTRY_CODES,
    is_eu_member_state,
    normalise,
    parse,
)
from tests.customers.vat_number_cases import ALL_CASES, MALFORMED, WELL_FORMED, ParseCase


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.raw or "<empty>")
def test_parse_matches_the_case_table(case: ParseCase) -> None:
    parsed = parse(case.raw)

    if case.expected is None:
        assert parsed is None, case.why
    else:
        assert parsed is not None, case.why
        assert parsed.value == case.expected, case.why


def test_parse_of_none_is_none() -> None:
    """A customer without a VAT number is entirely ordinary - a domestic
    consumer has none - so this is a value, not an error.
    """
    assert parse(None) is None


@pytest.mark.parametrize("case", WELL_FORMED, ids=lambda case: case.raw)
def test_the_prefix_and_the_national_part_recombine(case: ParseCase) -> None:
    """`value` is the two halves joined, and the halves are what a VIES lookup
    is built from - the REST path is /ms/{country}/vat/{number}. A split that
    lost a character would produce a confident "not found" for a real number.
    """
    parsed = parse(case.raw)
    assert parsed is not None
    assert parsed.country_code + parsed.national_number == parsed.value
    assert len(parsed.country_code) == 2


def test_normalise_never_raises_on_anything() -> None:
    """It is applied before anything has decided the input is a VAT number, so
    a raise here would turn a half-typed field into a failed request.
    """
    for junk in ("", "   ", "???", "NL", "\n\t", "-.-.-", "🙂"):
        assert isinstance(normalise(junk), str)


def test_normalise_is_idempotent() -> None:
    """Applied at the route, in the service, and again in the repository's
    comparison. If it were not idempotent, one number would acquire two
    spellings depending on how many layers it had passed through - and two
    spellings is two customer records.
    """
    for case in WELL_FORMED:
        once = normalise(case.raw)
        assert normalise(once) == once


def test_greece_is_el_and_not_gr() -> None:
    """The single most common VIES integration bug: keying the lookup on the
    ISO 3166 country code. `GR` is refused rather than silently rewritten, so
    the mistake surfaces where it is made.
    """
    assert is_eu_member_state("EL")
    assert not is_eu_member_state("GR")
    assert "EL" in EU_VAT_COUNTRY_CODES
    assert "GR" not in EU_VAT_COUNTRY_CODES


def test_great_britain_is_out_and_northern_ireland_is_in() -> None:
    """Windsor Framework. A `GB` number is real and VIES cannot answer about
    it; treating it as invalid would put a wrong verdict on a correct number.
    """
    assert not is_eu_member_state("GB")
    assert is_eu_member_state("XI")


def test_the_dutch_format_does_not_apply_an_eleven_proef() -> None:
    """Since 2020 the Dutch number issued to a sole trader is RANDOM, so it no
    longer discloses a BSN and no longer satisfies the modulus-11 check the old
    one did. A checksum here would reject the numbers the Belastingdienst has
    been issuing for years.

    `NL000000000B01` cannot pass an eleven-proef. It parses, and must.
    """
    parsed = parse("NL000000000B01")
    assert parsed is not None
    assert parsed.value == "NL000000000B01"


def test_every_member_state_prefix_has_a_well_formed_example() -> None:
    """Guards the case table against the format table growing without the
    tests growing with it - a new member state whose pattern nothing exercises
    is a pattern nobody has read.
    """
    covered = {
        parsed.country_code for case in WELL_FORMED if (parsed := parse(case.raw)) is not None
    }
    assert covered == EU_VAT_COUNTRY_CODES, (
        "these VIES prefixes have no well-formed example in vat_number_cases: "
        f"{sorted(EU_VAT_COUNTRY_CODES - covered)}"
    )


def test_the_malformed_table_is_not_vacuous() -> None:
    """A negative case table that accidentally listed only valid numbers would
    pass every assertion above while proving nothing.
    """
    assert MALFORMED
    assert all(case.expected is None for case in MALFORMED)
