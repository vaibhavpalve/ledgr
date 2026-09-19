"""IBAN validation - SI-02: the supplier's own bank account, for the
EPC069-12 "pay by bank" QR code on an invoice PDF.

Lives here, not under `api.invoicing`, because it is used by two packages
that must not depend on each other: `api.onboarding` (where an
administration's IBAN is set) and `api.invoicing` (where it is read back to
build the QR code). Nesting it inside either would make the other reach
across a package boundary for one function - the same reason
`api.templates.repository` duplicates its `supplier()` query rather than
importing `api.invoicing.repository`'s.

--- Format AND checksum, unlike api.customers.vat_number ---

That module's docstring explains at length why a VAT number checksum would
be actively wrong (the Dutch number is deliberately random since 2020, to
stop disclosing a BSN). None of that applies here: IBAN's check digits
(ISO 7064 MOD 97-10) are a real, universally-applied validity property of
every IBAN ever issued, not a national quirk that changed - so unlike a VAT
number, an IBAN that fails the checksum is not a real IBAN, full stop. This
module can therefore say "invalid" with confidence a network call would add
nothing to, which is also why there is no "IBAN register" to consult the way
VIES exists for VAT numbers.

--- Why this matters more here than an ordinary form field would ---

A wrong IBAN on file does not just fail validation once - via SI-02 it goes
onto every invoice PDF as a QR code a customer's banking app reads and pays
automatically. A typo here is a payment sent to a stranger's account, not a
red squiggle under a text field, so this is checked with an actual checksum
rather than "does it look roughly right".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Iban", "normalise", "parse"]

#: Characters stripped before matching - the same posture
#: api.customers.vat_number.normalise takes: a person who copied an IBAN off
#: a bank statement or letterhead ("NL91 ABNA 0417 1643 00") has not made a
#: mistake.
_SEPARATORS = re.compile(r"[\s.\-]+")

#: ISO 13616's overall envelope: two letters, two check digits, then 11-30
#: more alphanumerics (the BBAN, whose exact length is set per country - see
#: the module docstring on why this does not enumerate all ~40 of them).
_SHAPE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


def normalise(value: str) -> str:
    """The compact upper-case form this is stored and rendered in. Never
    raises - mirrors api.customers.vat_number.normalise exactly.
    """
    return _SEPARATORS.sub("", value).strip().upper()


@dataclass(frozen=True, slots=True)
class Iban:
    """A syntactically well-formed IBAN whose check digits are correct."""

    #: The compact form - "NL91ABNA0417164300" - what is stored and what
    #: goes into the QR payload.
    value: str

    @property
    def country_code(self) -> str:
        return self.value[:2]

    def __str__(self) -> str:
        return self.value


def _mod_97(rearranged: str) -> int:
    """ISO 7064 MOD 97-10, computed digit group by digit group so the
    number never has to exist as a single Python int wider than needed -
    it would work either way at these lengths, but this is the textbook
    algorithm as published, not a shortcut that happens to agree with it.
    """
    remainder = 0
    for char in rearranged:
        digit = int(char) if char.isdigit() else ord(char) - 55  # A=10 .. Z=35
        remainder = (remainder * (100 if digit > 9 else 10) + digit) % 97
    return remainder


def parse(value: str | None) -> Iban | None:
    """An `Iban`, or None if the string is not a valid one - shape AND
    checksum, both. Never raises, for the same reason
    api.customers.vat_number.parse never does: an administration without a
    bank account on file yet is an ordinary state, not an error.
    """
    if value is None:
        return None

    compact = normalise(value)
    if not _SHAPE.match(compact):
        return None

    # Move the first four characters (country + check digits) to the end,
    # then letters -> two digits each (A=10 .. Z=35), then mod 97 must be 1.
    rearranged = compact[4:] + compact[:4]
    if _mod_97(rearranged) != 1:
        return None

    return Iban(value=compact)
