"""api.customers.address - the one-way bridge from 0039's parts to 0037's blob.

Two things under test: the format a Dutch invoice actually carries, and the
refusal to render an address Wet OB art. 35a(1)(e) would not accept.

The second is the one with consequences. A partially rendered address is a
plausible-looking line missing something the statute requires, discovered by
FR-AR-003's gate a step later with no idea which field was at fault.
"""

from __future__ import annotations

import pytest

from api.customers.address import AddressIsIncomplete, PostalAddress, format_address

COMPLETE = PostalAddress(
    address_line1="Damrak 70",
    address_line2=None,
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
)


def test_a_domestic_address_omits_the_country_line() -> None:
    """The postal convention, and useful besides: an invoice to a Dutch
    customer that printed NEDERLAND reads as though somebody misconfigured it.
    """
    assert format_address(COMPLETE) == "Damrak 70\n1012 LM  Amsterdam"


def test_the_postcode_and_city_share_a_line_with_two_spaces() -> None:
    """PostNL's convention. Two spaces rather than one, because a single space
    disappears into a proportional font on a rendered PDF.
    """
    assert "1012 LM  Amsterdam" in format_address(COMPLETE)


def test_a_second_line_is_kept_where_there_is_one() -> None:
    address = PostalAddress(
        address_line1="Damrak 70",
        address_line2="Unit 3",
        postal_code="1012 LM",
        city="Amsterdam",
        country="NL",
    )
    assert format_address(address) == "Damrak 70\nUnit 3\n1012 LM  Amsterdam"


def test_a_foreign_address_carries_the_country_code() -> None:
    """The bare ISO code rather than a name: a NAME would need translating, and
    into the DESTINATION's language rather than the reader's - a detail easy to
    get backwards, producing mail a foreign sorting office cannot route.
    """
    address = PostalAddress(
        address_line1="Unter den Linden 1",
        address_line2=None,
        postal_code="10117",
        city="Berlin",
        country="DE",
    )
    assert format_address(address) == "Unter den Linden 1\n10117  Berlin\nDE"


def test_the_supplier_country_decides_what_counts_as_foreign() -> None:
    """A parameter rather than a constant, so a future non-NL administration
    does not print its own country on every domestic invoice.
    """
    assert "NL" in format_address(COMPLETE, supplier_country="DE")
    assert "NL" not in format_address(COMPLETE, supplier_country="NL")


@pytest.mark.parametrize(
    ("missing_field", "address"),
    [
        (
            "address_line1",
            PostalAddress(None, None, "1012 LM", "Amsterdam", "NL"),
        ),
        (
            "postal_code",
            PostalAddress("Damrak 70", None, None, "Amsterdam", "NL"),
        ),
        (
            "city",
            PostalAddress("Damrak 70", None, "1012 LM", None, "NL"),
        ),
    ],
)
def test_an_incomplete_address_refuses_to_render(
    missing_field: str, address: PostalAddress
) -> None:
    with pytest.raises(AddressIsIncomplete) as caught:
        format_address(address)

    assert missing_field in caught.value.missing
    assert not address.is_complete


def test_a_blank_field_is_missing_rather_than_present() -> None:
    """ "Is the address blank" is satisfied by a single space, which is exactly
    why 0038's header argues for structured parts. The same argument has to
    hold here or the structure buys nothing.
    """
    address = PostalAddress("Damrak 70", None, "  ", "Amsterdam", "NL")

    assert not address.is_complete
    assert "postal_code" in address.missing_statutory_parts


def test_every_missing_part_is_reported_at_once() -> None:
    """A form marks all three inputs rather than revealing them one save at a
    time - the shape `api.invoicing.statutory` reports its failures in.
    """
    address = PostalAddress(None, None, None, None, "NL")

    assert address.missing_statutory_parts == ("address_line1", "postal_code", "city")


def test_line_two_is_genuinely_optional() -> None:
    """A suite or PO box. Not statutory, so its absence must not block an
    invoice - which is why it is excluded from missing_statutory_parts.
    """
    assert COMPLETE.address_line2 is None
    assert COMPLETE.is_complete


def test_surrounding_whitespace_is_trimmed_from_the_rendered_block() -> None:
    address = PostalAddress("  Damrak 70 ", "  Unit 3 ", " 1012 LM ", " Amsterdam ", "nl")

    assert format_address(address) == "Damrak 70\nUnit 3\n1012 LM  Amsterdam"
