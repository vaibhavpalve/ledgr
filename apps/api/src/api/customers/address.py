"""Rendering a customer's structured address onto a statutory document.

Migration 0039 stores the address in parts because EN 16931 needs the parts
(FR-AR-005's Peppol, P2) and because FR-AR-003's gate has to ask "are street,
postcode and city all present", which a single blob cannot answer.

Migration 0037 stores ONE text column on the invoice, snapshotted and frozen.

This module is the one-way bridge between them. The rendered string is never
parsed back - if it were, the parts would be the derived thing and the blob the
source of truth, which is the arrangement 0038's header rejects at length.

--- The format is the Dutch postal convention ---

    Damrak 70
    Unit 3
    1012 LM  Amsterdam
    DEUTSCHLAND        <- only when it is not the supplier's own country

PostNL puts the postcode before the city with two spaces between. The country
line is omitted for domestic post, which is both the convention and useful: an
invoice to a Dutch customer that printed "NEDERLAND" reads as though somebody
configured it wrong.

The country is rendered as its bare ISO code rather than a name, because a
NAME would need translating (FR-LOC-001) and the language it should be
translated into is the DESTINATION's, not the reader's - a detail that is
easy to get backwards and produces mail that a foreign sorting office cannot
route. The code is unambiguous in every language, which is why the Universal
Postal Union permits it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PostalAddress", "format_address", "AddressIsIncomplete"]


class AddressIsIncomplete(ValueError):
    """Refuses to render an address the statute would not accept.

    A raise rather than a partial string, because the caller is about to write
    the result onto a document that outlives the request. An address rendered
    from a customer with no city is a plausible-looking line that is missing
    something Wet OB art. 35a(1)(e) requires, and it would be discovered by
    the FR-AR-003 gate a step later with no idea which field was at fault.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "this customer's address cannot be rendered onto an invoice; it is "
            f"missing: {', '.join(missing)} (FR-AR-003, Wet OB art. 35a(1)(e))"
        )


@dataclass(frozen=True, slots=True)
class PostalAddress:
    """Mirrors the address columns of migration 0039's `customer`."""

    address_line1: str | None
    address_line2: str | None
    postal_code: str | None
    city: str | None
    #: ISO 3166-1 alpha-2.
    country: str

    @property
    def missing_statutory_parts(self) -> tuple[str, ...]:
        """Which of the three parts the statute names are absent.

        Line 2 is genuinely optional (a suite or PO box), so it is not here.
        Reported as a tuple rather than a boolean so a form can mark the
        specific inputs, which is the same shape `api.invoicing.statutory`
        reports its own failures in.
        """
        return tuple(
            name
            for name, value in (
                ("address_line1", self.address_line1),
                ("postal_code", self.postal_code),
                ("city", self.city),
            )
            if value is None or not value.strip()
        )

    @property
    def is_complete(self) -> bool:
        return not self.missing_statutory_parts


def format_address(address: PostalAddress, *, supplier_country: str = "NL") -> str:
    """The single text block migration 0037 snapshots onto the invoice.

    `supplier_country` decides whether the country line appears - it is
    omitted when sender and recipient are in the same country, which is the
    postal convention. It defaults to NL because every administration in this
    system files a Dutch return, but it is a parameter rather than a constant
    so that a future non-NL administration (PRD §3, out of scope) does not
    silently print its own country on every domestic invoice.
    """
    missing = address.missing_statutory_parts
    if missing:
        raise AddressIsIncomplete(missing)

    # Narrowed by the check above; asserted for mypy rather than assumed.
    assert address.address_line1 is not None
    assert address.postal_code is not None
    assert address.city is not None

    lines = [address.address_line1.strip()]
    if address.address_line2 and address.address_line2.strip():
        lines.append(address.address_line2.strip())
    # Two spaces: PostNL's own convention, and it survives a proportional font
    # in a way a single space does not.
    lines.append(f"{address.postal_code.strip()}  {address.city.strip()}")

    if address.country.strip().upper() != supplier_country.strip().upper():
        lines.append(address.country.strip().upper())

    return "\n".join(lines)
